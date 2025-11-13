import json, decimal, asyncio, time
import modules.launchpad as lp
import modules.vaults as v
import modules.markets as m
import modules.lp as amm

decimal.getcontext().prec = 50

CONTRACTS = {
    "ROUTER": "0x4658c8879Ec0dD9063EE12371Bd70eA694F4284d",
    "VAULTS": "0x91C46195325633be39062Fe907323D5c9d1050Ae",
}
ADDRS = [a.lower() for a in CONTRACTS.values()]
EVENT_SIGS = {
    "0xaf714121669901a97bedd215ae52bf255f4b5ecb9b5baa168800e5bdcc32c21a": "MC",
    "0x9adcf0ad0cda63c4d50f26a48925cf6405df27d422a39c456b5f03f661c82982": "TR",
    
    "0xc06e2355c9da33769608ef0b4a541792c64990d67d8fe190ccc295daffa0a61c": "VD",
    "0x4e2ca0515ed1aef1395f66b5303bb5d6f1bf9d61a353fa53f73f8ac9973fa9f6": "VDP",
    "0xebff2602b3f468259e1e99f613fed6691f3a6526effe6ef3e768ba7ae7a36c4f": "VWD",
    "0x44427e3003a08f22cf803894075ac0297524e09e521fc1c15bc91741ce3dc159": "VLOCK",
    "0x7e6adfec7e3f286831a0200a754127c171a2da564078722cb97704741bbdb0ea": "VUNLOCK",
    "0x13607bf9d2dd20e1f3a7daf47ab12856f8aad65e6ae7e2c75ace3d0c424a40e8": "VCLOSE",
    
    "0x2f00e3cdd69a77be7ed215ec7b2a36784dd158f921fca79ac29deffa353fe6ee": "MINT",
    "0xd3986fdc78865c06fb072387efddb45772a87fe2105e598db99f085be3d05b84": "BURN",
    "0xc95e30a514d4115dee44b3ba17b2fc114501726562d4c5f2663c06f42df8f1e7": "SYNC",
        
    "0x24ad3570873d98f204dae563a92a783a01f6935a8965547ce8bf2cadd2c6ce3b": "TC",
    "0xc367a2f5396f96d105baaaa90fe29b1bb18ef54c712964410d02451e67c19d3e": "LT",
}
TOPICS = list(EVENT_SIGS.keys())
PARSERS = {
    "MC": m.parse_market_created,
    "TR": m.parse_trade,
    "VD": v.parse_vault_created,
    "VDP": v.parse_vault_deposit,
    "VWD": v.parse_vault_withdraw,
    "VLOCK": v.parse_vault_lock,
    "VUNLOCK": v.parse_vault_unlock,
    "VCLOSE": v.parse_vault_close,
    "MINT": amm.parse_mint,
    "BURN": amm.parse_burn,
    "SYNC": amm.parse_sync,
    "LT": lp.parse_launchpad_trade,
    "TC": lp.parse_token_created,
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
