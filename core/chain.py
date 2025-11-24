import json, decimal, asyncio, time
import modules.launchpad as lp
import modules.nadfun as n

decimal.getcontext().prec = 100

# transfer parser idk where else to put ts
def _parse_transfer(addr: str, tops: list[str], data_no0x: str) -> dict:
    from_addr = lp.to_addr(tops[1]) if len(tops) > 1 else "0x" + "0" * 40
    to_addr = lp.to_addr(tops[2]) if len(tops) > 2 else "0x" + "0" * 40

    amount = 0
    if data_no0x:
        try:
            amount = int(data_no0x[:64], 16)
        except ValueError:
            amount = 0

    return {
        "token": addr.lower(),
        "from": from_addr.lower(),
        "to": to_addr.lower(),
        "amount": amount,
    }

CONTRACTS = {
    "NADFUN": "0xA7283d07812a02AFB7C09B60f8896bCEA3F90aCE",
}
ADDRS = [a.lower() for a in CONTRACTS.values()]
EVENT_SIGS = {   
    "0xd37e3f4f651fe74251701614dbeac478f5a0d29068e87bbe44e5026d166abca9": "NFC",
    "0x00a7ba871905cb955432583640b5c9fc6bdd27d36884ab2b5420839224638862": "NFB",
    "0x0eb25df0e2137de8ce042eeaf39080d25f0c8d451372c99db69a4c0a298d0fa1": "NFS",
    "0xfd4bb47bd45abdbdb2ecd61052c9571773f9cde876e2a7745f488c20b30ab10a": "NFSYNC",
    "0xa1cae252e597e19f398a442722a17a17e62d17f9d4f3656786e18aabcd428908": "NFT",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "V3SWAP",
    
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef": "TF",
}
TOPICS = list(EVENT_SIGS.keys())
PARSERS = {
    "NFC": n.parse_nadfun_token_created,
    "NFB": n.parse_nadfun_buy,
    "NFS": n.parse_nadfun_sell,
    "NFSYNC": n.parse_nadfun_sync,
    "NFT": n.parse_nadfun_graduated,
    "V3SWAP": n.parse_v3_trade,
    "TF": _parse_transfer,
}
WS_URL = "wss://rpc-mainnet.monadinfra.com"
_RPC_MAX_RPS = 20
_last_rpc_ts = 0.0
_rpc_lock = asyncio.Lock()

# wait for the jsonrpc response with matching id, raising if it contains an error
async def ack(ws, rid):
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == rid:
            if "error" in resp:
                raise RuntimeError(resp)
            return resp

# applies rate limit so rpc calls dont exceed rps limit
async def rate_gate() -> None:
    global _last_rpc_ts
    async with _rpc_lock:
        now = time.monotonic()
        wait = 1 / _RPC_MAX_RPS - (now - _last_rpc_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_rpc_ts = time.monotonic()
