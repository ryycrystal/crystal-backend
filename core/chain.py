import json, decimal, asyncio, time
from decimal import Decimal
import modules.launchpad as lp
import modules.vaults as v
import modules.markets as m

decimal.getcontext().prec = 50

CONTRACTS = {
    "ROUTER": "0x4658c8879Ec0dD9063EE12371Bd70eA694F4284d",
    "VAULTS": "0x91C46195325633be39062Fe907323D5c9d1050Ae",
}
ADDRS = [a.lower() for a in CONTRACTS.values()]
EVENT_SIGS = {
    "0x24ad3570873d98f204dae563a92a783a01f6935a8965547ce8bf2cadd2c6ce3b": "TC",
    "0xc367a2f5396f96d105baaaa90fe29b1bb18ef54c712964410d02451e67c19d3e": "LT",
    "0x9adcf0ad0cda63c4d50f26a48925cf6405df27d422a39c456b5f03f661c82982": "TR",
    "0xc06e2355c9da33769608ef0b4a541792c64990d67d8fe190ccc295daffa0a61c": "VC",
    "": "MC",
}
TOPICS = list(EVENT_SIGS.keys())
PARSERS = {
    "LT": lp.parse_launchpad_trade,
    "TC": lp.parse_token_created,
    "TR": m.parse_trade,
    "VC": v.parse_vault_created,
    "MC": m.parse_market_created,
}

WS_URL = "wss://testnet-rpc.monad.xyz"

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
