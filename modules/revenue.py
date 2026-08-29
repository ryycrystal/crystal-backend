import json
import os
import time
import urllib.request

CRYSTAL_FEE_ADDRESS = os.getenv("CRYSTAL_FEE_ADDRESS", "0x565e9c68fc827958551ede5757461959206ab0bd").lower()


def _rpc(method: str, params: list):
    rpc = os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(rpc, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    body = json.load(urllib.request.urlopen(req, timeout=20))
    if body.get("error"):
        return None
    return body.get("result")


def fetch_fee_balance() -> tuple[int, int] | None:
    head = _rpc("eth_blockNumber", [])
    if not isinstance(head, str):
        return None
    try:
        block = int(head, 16)
    except ValueError:
        return None
    bal = _rpc("eth_getBalance", [CRYSTAL_FEE_ADDRESS, hex(block)])
    if not isinstance(bal, str):
        return None
    try:
        return block, int(bal, 16)
    except ValueError:
        return None


def sample_revenue(storage_module) -> dict | None:
    got = fetch_fee_balance()
    if got is None:
        return None
    block, balance = got
    try:
        price = storage_module.get_mon_price_usd()
    except Exception:
        price = None
    try:
        return storage_module.record_revenue_sample(block, int(time.time()), balance, price or 0)
    except Exception as e:
        print(f"[Revenue] sample failed: {e!r}", flush=True)
        return None
