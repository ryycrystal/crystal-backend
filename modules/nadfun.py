from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from decimal import Decimal, getcontext
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

getcontext().prec = 100

_PENDING_SYNC: Dict[str, dict] = {}
_FAILED_HOSTS: Dict[str, float] = {}
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
_BACKOFF_SCHEDULE = [30, 60, 120, 240, 480]

V2_CREATE_TOPIC = "0xac11ceed5187dce8e72afc92116e2aebbbd4fb263cb4021374a5df1b90c89936"
V2_BUY_TOPIC = "0x89f5adc174562e07c9c9b1cae7109bbecb21cf9d1b2847e550042b8653c54a0e"
V2_SELL_TOPIC = "0xa082022e93cfcd9f1da5f9236718053910f7e840da080c789c7845698dc032ff"
V2_SNIPING_PENALTY_TOPIC = "0x9cf337bf5592ea341168705a5dd168d5d26aaedb4d4725f6f13dd30aeae3322d"
V2_PAIR_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


def _pop_metadata_batch(limit: int) -> list[tuple[str, str]]:
    if not METADATA_QUEUE:
        return []
    n = min(max(int(limit or 1), 1), len(METADATA_QUEUE))
    batch = METADATA_QUEUE[:n]
    del METADATA_QUEUE[:n]
    return batch


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

def _get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(_CONCURRENCY_LIMIT)
    return _SEMAPHORE

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
            return ({
                "token": token,
                "name": meta.get("name", ""),
                "symbol": meta.get("symbol", ""),
                "description": meta.get("description", ""),
                "image_uri": meta.get("image_uri", ""),
                "website": meta.get("website", ""),
                "twitter": meta.get("twitter", ""),
                "telegram": meta.get("telegram", ""),
            }, token, token_uri)
        except Exception as e:
            if host:
                _FAILED_HOSTS[host] = time.monotonic()
            return (None, token, token_uri)

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

async def start_metadata_worker(storage_module) -> None:
    global _METADATA_WORKER_RUNNING, _STORAGE_MODULE
    if _METADATA_WORKER_RUNNING:
        return
    _METADATA_WORKER_RUNNING = True
    _STORAGE_MODULE = storage_module

    async def worker():
        while True:
            try:
                now = time.monotonic()
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

                                                       
def _to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

                                                                                   
def _word(data_hex: str, index: int) -> int:
    if not data_hex:
        return 0
    start = index * 64
    end = start + 64
    if end > len(data_hex):
        return 0
    return int(data_hex[start:end], 16)

                                                                     
def _chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))

                                                                             
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

                                                                     
def _int256_from_hex(x: str) -> int:
    if x.startswith("0x"):
        x = x[2:]
    if not x:
        return 0
    n = int(x, 16)
    if n >= 2**255:
        n -= 2**256
    return n

              
                    
                  
                 
                
                  
                    
                              
                                
                             
    
                                                
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

    try:
        last_price_native = Decimal("90000") / Decimal("1073000191")
    except Exception:
        last_price_native = Decimal(0)

    return {
        "token": token,
        "creator": creator,
        "pool": pool,
        "quote_token": quote_token,
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

           
                
                   
                            
                              
    
                                                                                         
def _parse_nadfun_trade(
    _addr: str,
    topics: list[str],
    data_no0x: str,
    is_buy: bool,
) -> Optional[dict]:
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


def parse_nadfun_buy(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    return _parse_nadfun_trade(_addr, topics, data_no0x, True)

            
                
                   
                            
                              
    
                                                                                         
def parse_nadfun_sell(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    return _parse_nadfun_trade(_addr, topics, data_no0x, False)

                             
                                            
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
