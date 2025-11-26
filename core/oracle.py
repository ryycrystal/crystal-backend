from __future__ import annotations

import json
import threading
import urllib.request
from decimal import Decimal

from state import RPC_HTTP

STORK_CONTRACT = "0xacC0a0cF13571d30B4b8637996F5D6D774d4fd62"
STORK_ASSET_KEY = "0xa4f6b07ae0c89e3f3cc03c1badcc3e9adffdf7206bafcd56d142979800887385"
STORK_FN_SELECTOR = "0xf69058c1"

_call_data: str | None = None
_lock = threading.Lock()
_last_price = Decimal("0.03")
last_block: int | None = None

def _build_call_data() -> str:
    global _call_data
    
    if _call_data is not None:
        return _call_data
    
    if not STORK_FN_SELECTOR or not STORK_ASSET_KEY:
        raise RuntimeError("[Oracle] Stork Configuration Missing")
    
    selector = STORK_FN_SELECTOR.removeprefix("0x")
    key = STORK_ASSET_KEY.removeprefix("0x").rjust(64, "0")
    _call_data = "0x" + selector + key
    return _call_data

def fetch_mon_price(block: int | None = None) -> Decimal:
    global _last_price, _last_block
    
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": STORK_CONTRACT,
                "data": _build_call_data(),
            },
            hex(block) if block is not None else "latest"
        ],
    }
    
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        RPC_HTTP,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode())
    except Exception:
        return _last_price
    
    if "error" in out:
        return _last_price
    
    result = out.get("result") or ""
    if not result or result == "0x":
        return _last_price
    
    try:
        price_wei = int(result[66:], 16)
        if price_wei <= 0:
            return _last_price
        price = Decimal(price_wei) / Decimal(10**18)
    except Exception:
        return _last_price
    
    with _lock:
        _last_price = price
        _last_block = block
    
    return price