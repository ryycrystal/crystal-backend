import json, decimal, asyncio, time
from decimal import Decimal

decimal.getcontext().prec = 50


WS_URL = "wss://testnet-rpc.monad.xyz"
CONTRACTS = {
    "ROUTER": "0x363D3D0ECe8995DfB9a4d6D7B76A6a4eFA70B7D7",
}
ADDRS = [a.lower() for a in CONTRACTS.values()]
EVENT_SIGS = {
    "0x24ad3570873d98f204dae563a92a783a01f6935a8965547ce8bf2cadd2c6ce3b": "TC",
    "0xc367a2f5396f96d105baaaa90fe29b1bb18ef54c712964410d02451e67c19d3e": "LT",
}
TOPICS = list(EVENT_SIGS.keys())

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]


def chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))


def parse_launchpad_trade(addr, tops, data):
    token = to_addr(tops[1]).lower()
    user = to_addr(tops[2]).lower()

    words = list(chunks(data, 64))
    is_buy = int(words[0], 16) != 0 if len(words) > 0 else False
    amount_in = int(words[1], 16) if len(words) > 1 else 0
    amount_out= int(words[2], 16) if len(words) > 2 else 0

    return {
        "token": token,
        "user": user,
        "is_buy": is_buy,
        "amount_in": amount_in,
        "amount_out": amount_out,
    }


def parse_token_created(addr, tops, data):
    token = to_addr(tops[1]).lower()
    creator = to_addr(tops[2]).lower()
    return {
        "token": token,
        "creator": creator,
    }


PARSERS = {
    "LT": parse_launchpad_trade,
    "TC": parse_token_created,
}

async def ack(ws, rid):
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == rid:
            if "error" in resp:
                raise RuntimeError(resp)
            return resp

_RPC_MAX_RPS = 20
_last_rpc_ts = 0.0
_rpc_lock = asyncio.Lock()

async def rate_gate() -> None:
    global _last_rpc_ts
    async with _rpc_lock:
        now = time.monotonic()
        wait = 1 / _RPC_MAX_RPS - (now - _last_rpc_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_rpc_ts = time.monotonic()