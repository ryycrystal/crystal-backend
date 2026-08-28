from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from decimal import Decimal, getcontext
from urllib.parse import urlparse

import httpx

getcontext().prec = 100

_PENDING_SYNC: dict[str, dict] = {}
_FAILED_HOSTS: dict[str, float] = {}
METADATA_QUEUE: list[tuple[str, str]] = []
_METADATA_CLIENT: httpx.AsyncClient | None = None

_CONCURRENCY_LIMIT = 100
_METADATA_BATCH_SIZE = int(os.getenv("METADATA_BATCH_SIZE", "500"))
_METADATA_LOG_EVERY = int(os.getenv("METADATA_LOG_EVERY", "1000"))
_REQUEST_TIMEOUT = 1.0
_HOST_BLACKLIST_DURATION = 30
_SEMAPHORE: asyncio.Semaphore | None = None

_RETRY_QUEUE: deque[tuple[str, str, int, float]] = deque()
_MAX_RETRY_QUEUE_SIZE = 10000
_MAX_RETRIES = 5

# tokens per pass whose uri is read back off chain, bounded so the recovery of a
# large historical backlog is spread out instead of hammering the rpc at once
_URI_RECOVERY_BATCH = int(os.getenv("URI_RECOVERY_BATCH", "100"))
_BACKOFF_SCHEDULE = [30, 60, 120, 240, 480]

V2_CREATE_TOPIC = "0xac11ceed5187dce8e72afc92116e2aebbbd4fb263cb4021374a5df1b90c89936"
V2_BUY_TOPIC = "0x89f5adc174562e07c9c9b1cae7109bbecb21cf9d1b2847e550042b8653c54a0e"
V2_SELL_TOPIC = "0xa082022e93cfcd9f1da5f9236718053910f7e840da080c789c7845698dc032ff"
V2_SNIPING_PENALTY_TOPIC = "0x9cf337bf5592ea341168705a5dd168d5d26aaedb4d4725f6f13dd30aeae3322d"
V2_PAIR_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V2_PAIR_SYNC_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

_PENDING_PAIR_SYNC: dict[str, tuple[int, int]] = {}


# take up to limit tokens off the pending metadata queue
def _pop_metadata_batch(limit: int) -> list[tuple[str, str]]:
    if not METADATA_QUEUE:
        return []
    n = min(max(int(limit or 1), 1), len(METADATA_QUEUE))
    batch = METADATA_QUEUE[:n]
    del METADATA_QUEUE[:n]
    return batch


# shared http client for metadata fetches
# metadata key names were never standardised. nadfun writes image_uri, and every
# other launchpad on the chain writes image, which is the erc-721 spelling. the
# first non empty one wins so a token's art is not dropped over which word the
# creator's launchpad happened to pick
def _first(meta: dict, *keys: str) -> str:
    for k in keys:
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


async def _get_metadata_client() -> httpx.AsyncClient:
    global _METADATA_CLIENT
    if _METADATA_CLIENT is None:
        limits = httpx.Limits(
            max_connections=200,
            max_keepalive_connections=100,
        )
        _METADATA_CLIENT = httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT,
            limits=limits,
            follow_redirects=True,
        )
    return _METADATA_CLIENT


# shared concurrency limiter for metadata fetches
def _get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(_CONCURRENCY_LIMIT)
    return _SEMAPHORE


# fetch and parse one tokens metadata json
async def fetch_metadata_single(token: str, token_uri: str) -> tuple[dict | None, str, str]:
    try:
        host = urlparse(token_uri).netloc
    except Exception:
        host = ""

    if host and host in _FAILED_HOSTS:
        if time.monotonic() - _FAILED_HOSTS[host] < _HOST_BLACKLIST_DURATION:
            return (None, token, token_uri)
        else:
            del _FAILED_HOSTS[host]

    sem = _get_semaphore()
    async with sem:
        try:
            client = await _get_metadata_client()
            resp = await client.get(token_uri)
            resp.raise_for_status()
            meta = resp.json()
            # a document that is not an object has nothing to read, but it is
            # not the host's fault either: returning empty leaves the token for
            # the next sweep instead of blacklisting everything else on that host
            if not isinstance(meta, dict):
                meta = {}
            return (
                {
                    "token": token,
                    "name": meta.get("name", ""),
                    "symbol": meta.get("symbol", ""),
                    "description": meta.get("description", ""),
                    "image_uri": _first(meta, "image_uri", "image", "imageUrl", "image_url"),
                    "website": meta.get("website", ""),
                    "twitter": meta.get("twitter", ""),
                    "telegram": meta.get("telegram", ""),
                },
                token,
                token_uri,
            )
        except Exception:
            if host:
                _FAILED_HOSTS[host] = time.monotonic()
            return (None, token, token_uri)


# drain a batch of queued metadata fetches concurrently
async def process_metadata_queue() -> list[dict]:
    if not METADATA_QUEUE:
        return []

    queue = _pop_metadata_batch(_METADATA_BATCH_SIZE)

    batch_with_attempts = [(token, uri, 0) for token, uri in queue]
    tasks = [fetch_metadata_single(token, uri) for token, uri, _ in batch_with_attempts]
    results = await asyncio.gather(*tasks)

    for (result, token, uri), (_, _, attempts) in zip(results, batch_with_attempts):
        if result is None and attempts < _MAX_RETRIES and len(_RETRY_QUEUE) < _MAX_RETRY_QUEUE_SIZE:
            backoff = _BACKOFF_SCHEDULE[min(attempts, len(_BACKOFF_SCHEDULE) - 1)]
            _RETRY_QUEUE.append((token, uri, attempts + 1, time.monotonic() + backoff))

    return [r for r, _, _ in results if r is not None]


_METADATA_WORKER_RUNNING = False
_STORAGE_MODULE = None


# background loop draining the metadata queue forever
async def start_metadata_worker(storage_module) -> None:
    global _METADATA_WORKER_RUNNING, _STORAGE_MODULE
    if _METADATA_WORKER_RUNNING:
        return
    _METADATA_WORKER_RUNNING = True
    _STORAGE_MODULE = storage_module

    # loop forever draining the metadata queue
    _SWEEP_EVERY_SECONDS = 300
    last_sweep = 0.0

    async def worker():
        nonlocal last_sweep
        while True:
            try:
                now = time.monotonic()
                # periodically recover tokens stranded by a restart or exhausted retries
                if now - last_sweep > _SWEEP_EVERY_SECONDS:
                    last_sweep = now
                    mod = _STORAGE_MODULE or storage_module
                    # recover uris first so tokens that never had one stored become
                    # visible to the sweep on this same pass
                    await recover_missing_uris(mod)
                    await sweep_missing_metadata(mod)
                    await sweep_pair_fees(mod)
                retry_batch = []
                temp_hold = []
                while _RETRY_QUEUE:
                    item = _RETRY_QUEUE.popleft()
                    token, uri, attempts, next_time = item
                    if next_time <= now:
                        retry_batch.append((token, uri, attempts))
                    else:
                        temp_hold.append(item)
                for item in temp_hold:
                    _RETRY_QUEUE.append(item)

                if retry_batch:
                    tasks = [fetch_metadata_single(token, uri) for token, uri, _ in retry_batch]
                    results = await asyncio.gather(*tasks)

                    for (result, token, uri), (_, _, attempts) in zip(results, retry_batch):
                        if result is None and attempts < _MAX_RETRIES and len(_RETRY_QUEUE) < _MAX_RETRY_QUEUE_SIZE:
                            backoff = _BACKOFF_SCHEDULE[min(attempts, len(_BACKOFF_SCHEDULE) - 1)]
                            _RETRY_QUEUE.append((token, uri, attempts + 1, time.monotonic() + backoff))

                    valid = [r for r, _, _ in results if r is not None]
                    if valid:
                        storage_module.update_token_metadata_batch(valid)

                qlen = len(METADATA_QUEUE)
                if qlen > 0:
                    batch_n = min(_METADATA_BATCH_SIZE, qlen)
                    print(f"[Metadata] Processing {batch_n}/{qlen} queued items...")
                    results = await process_metadata_queue()
                    if results:
                        print(f"[Metadata] Got {len(results)} results, saving...")
                        storage_module.update_token_metadata_batch(results)

                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[Metadata] Worker error: {e!r}")
                await asyncio.sleep(1.0)

    asyncio.create_task(worker())
    print("[Metadata] Background worker started")


# drain the metadata queue once and persist the results
async def process_metadata_queue_immediate(storage_module=None) -> int:
    if not METADATA_QUEUE:
        return 0

    queue = _pop_metadata_batch(_METADATA_BATCH_SIZE)

    if not queue:
        return 0

    batch_with_attempts = [(token, uri, 0) for token, uri in queue]
    tasks = [fetch_metadata_single(token, uri) for token, uri, _ in batch_with_attempts]
    results = await asyncio.gather(*tasks)

    for (result, token, uri), (_, _, attempts) in zip(results, batch_with_attempts):
        if result is None and attempts < _MAX_RETRIES and len(_RETRY_QUEUE) < _MAX_RETRY_QUEUE_SIZE:
            backoff = _BACKOFF_SCHEDULE[min(attempts, len(_BACKOFF_SCHEDULE) - 1)]
            _RETRY_QUEUE.append((token, uri, attempts + 1, time.monotonic() + backoff))

    valid_results = [r for r, _, _ in results if r is not None]

    store = storage_module or _STORAGE_MODULE
    if valid_results and store:
        store.update_token_metadata_batch(valid_results)

    return len(valid_results)


# last 20 bytes of a topic as a hex address
def _to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]


# read word n of a hex payload as an int
def _word(data_hex: str, index: int) -> int:
    if not data_hex:
        return 0
    start = index * 64
    end = start + 64
    if end > len(data_hex):
        return 0
    return int(data_hex[start:end], 16)


# split a hex payload into n char words
def _chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))


# follow an abi string offset and decode it
def _decode_string(data_hex: str, word_index: int) -> str:
    if not data_hex:
        return ""

    try:
        data = bytes.fromhex(data_hex)
    except ValueError:
        return ""

    base = word_index * 32
    if base + 32 > len(data):
        return ""

    offset = int.from_bytes(data[base : base + 32], "big")
    if offset + 32 > len(data):
        return ""

    length = int.from_bytes(data[offset : offset + 32], "big")
    start = offset + 32
    end = start + length
    if start > len(data):
        return ""
    if end > len(data):
        end = len(data)

    try:
        return data[start:end].decode("utf-8", errors="ignore")
    except Exception:
        return ""


# read a hex word as a signed int256
def _int256_from_hex(x: str) -> int:
    if x.startswith("0x"):
        x = x[2:]
    if not x:
        return 0
    n = int(x, 16)
    if n >= 2**255:
        n -= 2**256
    return n


# decode a nadfun tokencreated log into token creator and metadata uri
def parse_nadfun_token_created(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict:
    creator = _to_addr(topics[1]) if len(topics) > 1 else ""
    token = _to_addr(topics[2]) if len(topics) > 2 else ""
    pool = _to_addr(topics[3]) if len(topics) > 3 else ""

    is_v2 = topics and topics[0].lower() == V2_CREATE_TOPIC
    quote_token = _to_addr(data_no0x[:64]) if is_v2 and len(data_no0x) >= 64 else ""
    string_base = 1 if is_v2 else 0
    name = _decode_string(data_no0x, string_base)
    symbol = _decode_string(data_no0x, string_base + 1)
    token_uri = _decode_string(data_no0x, string_base + 2)

    description: str = ""
    image_uri: str = ""
    website: str = ""
    twitter: str = ""
    telegram: str = ""

    if token_uri and token:
        METADATA_QUEUE.append((token, token_uri))
        qlen = len(METADATA_QUEUE)
        if qlen == 1 or (_METADATA_LOG_EVERY > 0 and qlen % _METADATA_LOG_EVERY == 0):
            print(f"[Metadata] Queued {token[:10]}... queue size={qlen}")

    metadata_cid = ""

    # the creation price is v1 specific and each generation starts on a different
    # curve, so the adapter for the token's source owns it now
    last_price_native = Decimal(0)

    return {
        "token": token,
        "creator": creator,
        "pool": pool,
        "quote_token": quote_token,
        "name": name,
        "symbol": symbol,
        # carried so state can persist it. the queue above is in memory, so a
        # restart with a fetch still pending otherwise loses the uri for good and
        # the self healing sweep can never reach the token
        "token_uri": token_uri,
        "metadata_cid": metadata_cid,
        "description": description,
        "social1": website,
        "social2": twitter,
        "social3": telegram,
        "social4": "",
        "source": 1,
        "last_price_native": last_price_native,
    }


# decode a nadfun sync log and stash its reserves for the next trade
def parse_nadfun_sync(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict | None:
    if len(topics) < 2:
        return None

    token = _to_addr(topics[1])

    real_mon = _word(data_no0x, 0)
    real_token = _word(data_no0x, 1)
    virtual_mon = _word(data_no0x, 2)
    virtual_token = _word(data_no0x, 3)

    _PENDING_SYNC[token] = {
        "token": token,
        "real_native_reserve": real_mon,
        "real_token_reserve": real_token,
        "native_reserve": virtual_mon,
        "token_reserve": virtual_token,
    }

    return None


# take the stashed reserves for a token, zeros when none are pending
def _consume_sync_for_token(token: str) -> dict:
    sync = _PENDING_SYNC.pop(token.lower(), None)
    if not sync:
        return {
            "native_reserve": 0,
            "token_reserve": 0,
            "real_native_reserve": 0,
            "real_token_reserve": 0,
        }

    return {
        "native_reserve": int(sync.get("native_reserve", 0)),
        "token_reserve": int(sync.get("token_reserve", 0)),
        "real_native_reserve": int(sync.get("real_native_reserve", 0)),
        "real_token_reserve": int(sync.get("real_token_reserve", 0)),
    }


# shared decode for nadfun buy and sell logs
def _parse_nadfun_trade(
    _addr: str,
    topics: list[str],
    data_no0x: str,
    is_buy: bool,
) -> dict | None:
    if len(topics) < 3:
        return None

    is_v2 = topics[0].lower() in {V2_BUY_TOPIC, V2_SELL_TOPIC}
    if is_v2:
        token = _to_addr(topics[1])
        user = _to_addr(topics[2])
    else:
        user = _to_addr(topics[1])
        token = _to_addr(topics[2])

    actual_in = _word(data_no0x, 0)
    effective_out = _word(data_no0x, 1)

    sync = _consume_sync_for_token(token)

    return {
        "token": token,
        "user": user,
        "is_buy": is_buy,
        "amount_in": actual_in,
        "amount_out": effective_out,
        "native_reserve": sync["native_reserve"],
        "token_reserve": sync["token_reserve"],
    }


# decode a nadfun buy log
def parse_nadfun_buy(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict | None:
    return _parse_nadfun_trade(_addr, topics, data_no0x, True)


# decode a nadfun sell log
def parse_nadfun_sell(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict | None:
    return _parse_nadfun_trade(_addr, topics, data_no0x, False)


# decode a nadfun graduated log into token and its new pool
def parse_nadfun_graduated(
    _addr: str,
    topics: list[str],
    _data_no0x: str,
) -> dict:
    token = _to_addr(topics[1]).lower() if len(topics) > 1 else ""
    pool = _to_addr(topics[2]).lower() if len(topics) > 2 else ""
    return {"token": token, "pool": pool}


# decode a nadfun sniping penalty log
# v2 only anti sniping penalty, parsed and counted but deliberately not dispatched
#
# it is absent from the v1 abi nad.fun publishes so there is no authoritative
# definition to implement against, and it fired zero times in 20,000 blocks
#
# it cannot corrupt what matters: supply derives from CurveSync reserves and
# balances from erc20 transfers, both authoritative, so a penalty that moves
# tokens or mon shows up in the next sync and transfer anyway. the only thing it
# could skew is fee accounting, where a fee taken outside the reserve delta would
# not be captured
def parse_nadfun_sniping_penalty(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict:
    token = _to_addr(topics[1]).lower() if len(topics) > 1 else ""
    buyer = _to_addr(topics[2]).lower() if len(topics) > 2 else ""
    return {
        "token": token,
        "user": buyer,
        "sniping_fee": _word(data_no0x, 0),
        "penalty_bps": _word(data_no0x, 1),
    }


# decode a uniswap v3 swap log into both signed amounts
def parse_v3_trade(addr, tops, data):
    pool = addr.lower()
    sender = _to_addr(tops[1]).lower() if len(tops) > 1 else ""
    recipient = _to_addr(tops[2]).lower() if len(tops) > 2 else ""

    if isinstance(data, str) and data.startswith("0x"):
        hex_data = data[2:]
    else:
        hex_data = data

    words = list(_chunks(hex_data, 64))

    if len(words) < 5:
        return {
            "pool": pool,
            "sender": sender,
            "user": recipient,
            "amount0": 0,
            "amount1": 0,
            "sqrt_price_x96": 0,
        }

    amount0 = _int256_from_hex(words[0])
    amount1 = _int256_from_hex(words[1])

    try:
        sqrt_price_x96 = int(words[2], 16)
    except Exception:
        sqrt_price_x96 = 0

    return {
        "pool": pool,
        "sender": sender,
        "user": recipient,
        "amount0": amount0,
        "amount1": amount1,
        "sqrt_price_x96": sqrt_price_x96,
    }


# decode a v2 pair swap log into in and out amounts
def parse_v2_pair_swap(addr, tops, data):
    pool = addr.lower()
    sender = _to_addr(tops[1]).lower() if len(tops) > 1 else ""
    recipient = _to_addr(tops[2]).lower() if len(tops) > 2 else ""

    if isinstance(data, str) and data.startswith("0x"):
        hex_data = data[2:]
    else:
        hex_data = data

    words = list(_chunks(hex_data, 64))
    if len(words) < 4:
        return {
            "pool": pool,
            "sender": sender,
            "user": recipient,
            "amount0": 0,
            "amount1": 0,
            "sqrt_price_x96": 0,
        }

    try:
        amount0_in = int(words[0], 16)
        amount1_in = int(words[1], 16)
        amount0_out = int(words[2], 16)
        amount1_out = int(words[3], 16)
    except Exception:
        amount0_in = amount1_in = amount0_out = amount1_out = 0

    return {
        "pool": pool,
        "sender": sender,
        "user": recipient,
        "amount0": amount0_in - amount0_out,
        "amount1": amount1_in - amount1_out,
        "sqrt_price_x96": 0,
    }


# decode a univ2 pair sync log and stash its post swap reserves for the next swap,
# whose raw amounts are gross of the pair fee and cannot price the trade honestly
def parse_v2_pair_sync(addr, _tops, data):
    pool = (addr or "").lower()

    if isinstance(data, str) and data.startswith("0x"):
        hex_data = data[2:]
    else:
        hex_data = data

    words = list(_chunks(hex_data, 64))
    if len(words) < 2:
        return None

    try:
        reserve0 = int(words[0], 16)
        reserve1 = int(words[1], 16)
    except Exception:
        return None

    _PENDING_PAIR_SYNC[pool] = (reserve0, reserve1)
    return None


# take the stashed post swap reserves for a pair, zeros when none are pending
def consume_pair_sync(pool: str) -> tuple[int, int]:
    return _PENDING_PAIR_SYNC.pop((pool or "").lower(), (0, 0))


# read the stashed reserves without taking them, so reserve tracking can persist
# a sync that no swap will consume, which is exactly what a liquidity add or
# remove looks like on a pair
def peek_pair_sync(pool: str) -> tuple[int, int]:
    return _PENDING_PAIR_SYNC.get((pool or "").lower(), (0, 0))


# re-queue tokens that still have no metadata but do have a uri to fetch from.
# the queue is in memory, so a restart mid fetch strands a token forever otherwise,
# and retries are exhausted after about fifteen minutes with no path back
# tokens created before the uri was persisted have nothing for the sweep to fetch,
# so recover it from the token contract. bounded per pass to stay polite to the rpc
async def recover_missing_uris(storage_module, limit: int = _URI_RECOVERY_BATCH) -> int:
    from state import _TOKEN_URI_SELECTOR, _fetch_token_string

    try:
        tokens = await asyncio.to_thread(storage_module.tokens_missing_uri, limit)
    except Exception:
        return 0

    recovered = 0
    for token in tokens:
        try:
            uri = await asyncio.to_thread(_fetch_token_string, token, _TOKEN_URI_SELECTOR)
        except Exception:
            continue
        if not uri:
            continue
        try:
            await asyncio.to_thread(storage_module.set_token_uri, token, uri)
        except Exception:
            continue
        recovered += 1

    if recovered:
        print(f"[Metadata] recovered {recovered} token uris from chain", flush=True)
    return recovered


async def sweep_missing_metadata(storage_module, limit: int = 500) -> int:
    try:
        rows = await asyncio.to_thread(storage_module.tokens_missing_metadata, limit)
    except Exception:
        return 0

    pending = {t for t, _ in METADATA_QUEUE}
    pending |= {t for t, _, _, _ in _RETRY_QUEUE}

    queued = 0
    for token, uri in rows:
        if token in pending or not uri:
            continue
        METADATA_QUEUE.append((token, uri))
        queued += 1

    if queued:
        print(f"[Metadata] sweep re-queued {queued} tokens missing metadata", flush=True)
    return queued


# selectors derived from keccak256 of the signatures and verified against live pairs:
# a nad.fun pair exposes feeCollector(), a plain pair reverts, which classifies it
_FEE_COLLECTOR_SELECTOR = "0xc415b95c"
_GET_FEE_CONFIG_SELECTOR = "0x1e442b55"


# one raw eth_call, none on revert or transport failure
def _fee_rpc_call(to: str, data: str) -> str | None:
    import urllib.request

    rpc = os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
    try:
        req = urllib.request.Request(
            rpc, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        out = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception:
        return None
    res = out.get("result")
    return res if isinstance(res, str) and res != "0x" else None


# fee config for a pair straight off chain. ok=false means the pair has no fee
# collector, which is how a general dex pair is told apart from a nad.fun one
def fetch_pair_fee_config(pair: str) -> dict:
    pair = (pair or "").lower()
    out = {
        "pair": pair,
        "ok": False,
        "fee_collector": "",
        "base_token": "",
        "quote_token": "",
        "creator_fee_rate": 0,
        "curve_protocol_fee_rate": 0,
        "dex_protocol_fee_rate": 0,
    }
    fc = _fee_rpc_call(pair, _FEE_COLLECTOR_SELECTOR)
    if not fc or len(fc) < 42:
        return out
    collector = "0x" + fc[-40:]
    cfg = _fee_rpc_call(collector, _GET_FEE_CONFIG_SELECTOR + pair[2:].rjust(64, "0"))
    if not cfg or len(cfg) < 2 + 5 * 64:
        return out
    words = [cfg[2 + i * 64 : 2 + (i + 1) * 64] for i in range(5)]
    out.update(
        ok=True,
        fee_collector=collector,
        base_token="0x" + words[0][-40:],
        quote_token="0x" + words[1][-40:],
        creator_fee_rate=int(words[2], 16),
        curve_protocol_fee_rate=int(words[3], 16),
        dex_protocol_fee_rate=int(words[4], 16),
    )
    return out


# fetch fee config for migrated pairs the cache does not cover yet
async def sweep_pair_fees(storage_module, limit: int = 50) -> int:
    try:
        pairs = await asyncio.to_thread(storage_module.pairs_missing_fees, limit)
    except Exception:
        return 0
    done = 0
    for pair in pairs:
        cfg = await asyncio.to_thread(fetch_pair_fee_config, pair)
        try:
            await asyncio.to_thread(
                storage_module.upsert_pair_fees,
                pair,
                ok=cfg["ok"],
                fee_collector=cfg["fee_collector"],
                base_token=cfg["base_token"],
                quote_token=cfg["quote_token"],
                creator_fee_rate=cfg["creator_fee_rate"],
                curve_protocol_fee_rate=cfg["curve_protocol_fee_rate"],
                dex_protocol_fee_rate=cfg["dex_protocol_fee_rate"],
                fetched_at=int(time.time()),
            )
            done += 1
        except Exception:
            continue
    if done:
        print(f"[Fees] cached fee config for {done} pairs", flush=True)
    return done
