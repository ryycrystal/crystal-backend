"""How many decimals a token has, asked once and remembered.

Every path that turns a raw on-chain amount into a human one needs this, and until now only the spot
side had it: `crystal_markets.quote_decimals` is read from the market contract, while the launchpad
and pool paths knew a quote token's *address* and nothing about its scale, so they assumed 18. A
USDC-quoted pool therefore booked 13.21 USDC as 0.0000000000132 MON, off by 10^12, for nine months.

Decimals are immutable, so one `decimals()` call per token is enough for the life of the process and
of the database. A token whose decimals cannot be read returns None, and **the caller must refuse to
price rather than assume**: assuming is what turned a missing fact into a silently wrong number.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request

_DECIMALS_SELECTOR = "0x313ce567"  # decimals()
_MAX_SANE = 36

_cache: dict[str, int | None] = {}
_lock = threading.Lock()


def _rpc_decimals(token: str) -> int | None:
    rpc = os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": token, "data": _DECIMALS_SELECTOR}, "latest"],
    }
    try:
        req = urllib.request.Request(
            rpc, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        out = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception:
        return None
    res = out.get("result")
    if not isinstance(res, str) or len(res) < 3:
        return None
    try:
        value = int(res, 16)
    except ValueError:
        return None
    return value if 0 <= value <= _MAX_SANE else None


def decimals_for(token: str, cur=None, storage_module=None) -> int | None:
    """Decimals for `token`, from memory, then the database, then the chain. None when unknown.

    A miss is cached too, so a token that does not answer `decimals()` costs one call rather than one
    per swap. Restarting the process retries it.
    """
    addr = (token or "").lower()
    if not addr.startswith("0x") or len(addr) != 42:
        return None
    with _lock:
        if addr in _cache:
            return _cache[addr]

    value: int | None = None
    if storage_module is not None:
        try:
            raw = storage_module.get_token_decimals(addr, cur=cur)
        except Exception:
            # a missing table or a dead pool: fall through to the chain
            raw = None
        # a real integer only. int() would happily accept anything with __int__, which is how a
        # stubbed storage in a test quietly became "1 decimal" for every token on the chain.
        value = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        if value is not None and not (0 <= value <= _MAX_SANE):
            value = None

    if value is None:
        value = _rpc_decimals(addr)
        if value is not None and storage_module is not None:
            try:
                storage_module.upsert_token_decimals(addr, value, cur=cur)
            except Exception:
                pass

    with _lock:
        _cache[addr] = value
    return value


def prime(mapping: dict[str, int]) -> None:
    """Seed the cache, for tests and for the handful of assets known at boot."""
    with _lock:
        for token, value in mapping.items():
            _cache[(token or "").lower()] = value


def forget(token: str | None = None) -> None:
    with _lock:
        if token is None:
            _cache.clear()
        else:
            _cache.pop((token or "").lower(), None)
