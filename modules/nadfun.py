from __future__ import annotations

import asyncio
import time
from decimal import Decimal, getcontext
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

getcontext().prec = 100

_PENDING_SYNC: Dict[str, dict] = {}
_FAILED_HOSTS: Dict[str, float] = {}
METADATA_QUEUE: list[tuple[str, str]] = []
_METADATA_CLIENT: httpx.AsyncClient | None = None

async def _get_metadata_client() -> httpx.AsyncClient:
    global _METADATA_CLIENT
    if _METADATA_CLIENT is None:
        _METADATA_CLIENT = httpx.AsyncClient(timeout=2.0)
    return _METADATA_CLIENT

async def fetch_metadata_single(token: str, token_uri: str) -> dict | None:
    try:
        host = urlparse(token_uri).netloc
    except Exception:
        host = ""

    if host and host in _FAILED_HOSTS:
        if time.monotonic() - _FAILED_HOSTS[host] < 60:
            return None
        else:
            del _FAILED_HOSTS[host]

    try:
        client = await _get_metadata_client()
        resp = await client.get(token_uri)
        resp.raise_for_status()
        meta = resp.json()
        return {
            "token": token,
            "name": meta.get("name", ""),
            "symbol": meta.get("symbol", ""),
            "description": meta.get("description", ""),
            "image_uri": meta.get("image_uri", ""),
            "website": meta.get("website", ""),
            "twitter": meta.get("twitter", ""),
            "telegram": meta.get("telegram", ""),
        }
    except Exception as e:
        if host:
            _FAILED_HOSTS[host] = time.monotonic()
        return None

async def process_metadata_queue() -> list[dict]:
    if not METADATA_QUEUE:
        return []

    queue = METADATA_QUEUE.copy()
    METADATA_QUEUE.clear()

    tasks = [fetch_metadata_single(token, uri) for token, uri in queue]
    results = await asyncio.gather(*tasks)

    return [r for r in results if r is not None]


_METADATA_WORKER_RUNNING = False

async def start_metadata_worker(storage_module) -> None:
    global _METADATA_WORKER_RUNNING
    if _METADATA_WORKER_RUNNING:
        return
    _METADATA_WORKER_RUNNING = True

    async def worker():
        while True:
            try:
                qlen = len(METADATA_QUEUE)
                if qlen > 0:
                    print(f"[Metadata] Processing {qlen} items...")
                    results = await process_metadata_queue()
                    if results:
                        print(f"[Metadata] Got {len(results)} results, saving...")
                        storage_module.update_token_metadata_batch(results)
                    await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(1.0)
            except Exception as e:
                print(f"[Metadata] Worker error: {e!r}")
                await asyncio.sleep(2.0)

    asyncio.create_task(worker())
    print("[Metadata] Background worker started")

# 32-byte word or hex string into a 0x-prefixed address
def _to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

# read the 32-byte word at 'index' from a hex string (no 0x) interpreted as uint256
def _word(data_hex: str, index: int) -> int:
    if not data_hex:
        return 0
    start = index * 64
    end = start + 64
    if end > len(data_hex):
        return 0
    return int(data_hex[start:end], 16)

# # yield s in fixed-size n-character chunks (used for 32-byte words)
def _chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))

# decode an abi-encoded string from the calldata hex starting at 'word_index'
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

# parse a hex string into a signed 256-bit integer (two's complement)
def _int256_from_hex(x: str) -> int:
    if x.startswith("0x"):
        x = x[2:]
    if not x:
        return 0
    n = int(x, 16)
    if n >= 2**255:
        n -= 2**256
    return n

# CurveCreate(
#   address creator,
#   address token,
#   address pool,
#   string name,
#   string symbol,
#   string tokenURI,
#   uint256 virtualMonReserve,
#   uint256 virtualTokenReserve,
#   uint256 targetTokenAmount
# );
# into a flat dict for state.apply_token_created
def parse_nadfun_token_created(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict:
    creator = _to_addr(topics[1]) if len(topics) > 1 else ""
    token = _to_addr(topics[2]) if len(topics) > 2 else ""

    name = _decode_string(data_no0x, 0)
    symbol = _decode_string(data_no0x, 1)
    token_uri = _decode_string(data_no0x, 2)

    description: str = ""
    image_uri: str = ""
    website: str = ""
    twitter: str = ""
    telegram: str = ""

    if token_uri and token:
        METADATA_QUEUE.append((token, token_uri))
        print(f"[Metadata] Queued {token[:10]}... queue size={len(METADATA_QUEUE)}")

    metadata_cid = ""  # Will be populated by metadata fetch with actual image_uri

    try:
        last_price_native = Decimal("90000") / Decimal("1073000191")
    except Exception:
        last_price_native = Decimal(0)

    return {
        "token": token,
        "creator": creator,
        "name": name,
        "symbol": symbol,
        "metadata_cid": metadata_cid,
        "description": description,
        "social1": website,
        "social2": twitter,
        "social3": telegram,
        "social4": "",
        "source": 1,
        "last_price_native": last_price_native,
    }

# CurveSync(
#   address token,
#   uint256 realMonReserve,
#   uint256 realTokenReserve,
#   uint256 virtualMonReserve,
#   uint256 virtualTokenReserve
# );
# and stashes latest reserves to be used by the next buy/sell event for that token
def parse_nadfun_sync(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
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

# pop and return the last sync snapshot for 'token' or zeros if none
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

# CurveBuy(
#   address to, 
#   address token, 
#   uint256 actualAmountIn, 
#   uint256 effectiveAmountOut
# );
# into a flat trade dict, merges in latest sync reserves, for state.apply_launchpad_trade
def parse_nadfun_buy(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    if len(topics) < 3:
        return None

    user = _to_addr(topics[1])
    token = _to_addr(topics[2])

    actual_in = _word(data_no0x, 0)
    effective_out = _word(data_no0x, 1)

    sync = _consume_sync_for_token(token)

    return {
        "token": token,
        "user": user,
        "is_buy": True,
        "amount_in": actual_in,
        "amount_out": effective_out,
        "native_reserve": sync["native_reserve"],
        "token_reserve": sync["token_reserve"],
    }

# CurveSell(
#   address to, 
#   address token, 
#   uint256 actualAmountIn, 
#   uint256 effectiveAmountOut
# );
# into a flat trade dict, merges in latest sync reserves, for state.apply_launchpad_trade
def parse_nadfun_sell(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    if len(topics) < 3:
        return None

    user = _to_addr(topics[1])
    token = _to_addr(topics[2])

    actual_in = _word(data_no0x, 0)
    effective_out = _word(data_no0x, 1)

    sync = _consume_sync_for_token(token)

    return {
        "token": token,
        "user": user,
        "is_buy": False,
        "amount_in": actual_in,
        "amount_out": effective_out,
        "native_reserve": sync["native_reserve"],
        "token_reserve": sync["token_reserve"],
    }

# CurveGraduate(token, pool);
# with a pool param for state.apply_migrated
def parse_nadfun_graduated(
    _addr: str,
    topics: list[str],
    _data_no0x: str,
) -> dict:
    token = _to_addr(topics[1]).lower() if len(topics) > 1 else ""
    pool = _to_addr(topics[2]).lower() if len(topics) > 2 else ""
    return {
        "token": token, 
        "pool": pool
    }

# Swap(
#   address sender,
#   address recipient,
#   int256 amount0,
#   int256 amount1,
#   uint160 sqrtPriceX96,
#   uint128 liquidity,
#   int24 tick
# );
# parses uniswap v3-style Swap event into a dict consumable by state.apply_launchpad_trade (slightly diff shape)
# amount0 and amount1 are signed deltas, sqrt_price_x96 is the new sqrt price
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