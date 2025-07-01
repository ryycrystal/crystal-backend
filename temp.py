# log ingest and backfill

import asyncio, json, websockets

WS_URL = "wss://testnet-rpc.monad.xyz"

_CONTRACTS = [
    ("0xCd5455B24f3622A1CfEce944615AE5Bc8f36Ee18", "MONUSDC"),
    ("0x97fa0031E2C9a21F0727bcaB884E15c090eC3ee3", "sMONMON"),
    ("0x33C5Dc9091952870BD1fF47c89fA53D63f9729b6", "aprMONMON"),
    ("0xcB5ec6D6d0E49478119525E4013ff333Fc46B742", "shMONMON"),
    ("0x93cBC4b52358c489665680182f0056f4F23C76CD", "DAKMON"),
    ("0xf00A3bd942DC0e32d07048ED6255E281667784f6", "CHOGMON"),
    ("0x3051ec9feFaEc14F2bAB836FAb5A4c970A71874a", "YAKIMON"),
    ("0x9fA48CFB43829A932A227E4d7996e310ccf40E9C", "WETHUSDC"),
    ("0x45f7db719367bbf9E508D3CeA401EBC62fc732A9", "WBTCUSDC"),
    ("0x5a6f296032AaAE6737ed5896bC09D01dc2d42507", "WSOLUSDC"),
    ("0xCF16582dC82c4C17fA5b54966ee67b74FD715fB5", "USDTUSDC"),
    ("0x4e77071D619Aa164cA6427547aefA41AC51BE7A0", "ROUTER"),
]

ADDRS = [addr.lower() for addr, _ in _CONTRACTS]
ADDR_LABELS = {addr.lower(): label for addr, label in _CONTRACTS}

EVENT_SIGS = {
    "0xc3bcf95b5242764f3f2dc3e504ce05823a3b50c4ccef5e660d13beab2f51f2ca": "OrderFilled",
    "0x1c87843c023cd30242ff04316b77102e873496e3d8924ef015475cf066c1d4f4": "OrdersUpdated",
    "0xa4536bf9ecc2bb029e4c6500446c0543db158200d0041507373066024d5d2099": "UsernameUpdated",
    "0x9d05414fb79fac216c15606de5cc06664e91a254e4d5f57664d5f1beaf7fb7ef": "ReferralAdded",
}
TOPICS = list(EVENT_SIGS.keys())

def _print_log(result: dict) -> None:
    addr = result["address"].lower()
    label = ADDR_LABELS.get(addr, addr)
    topic0 = result["topics"][0].lower()
    event = EVENT_SIGS.get(topic0, topic0[:10])
    block = int(result["blockNumber"], 16)
    txhash = result["transactionHash"]
    print(f"[{label}] {event} - block {block} - hash {txhash}")
    

async def stream_logs() -> None:
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
            "params": ["newHeads"]
        }))
        heads_sub_id = (await _expect_ack(ws, 1))["result"]
        
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "eth_subscribe",
            "params": ["logs", {"address": ADDRS, "topics": [TOPICS]}]
        }))
        logs_sub_id = (await _expect_ack(ws, 2))["result"]
        
        print(f"heads sub: {heads_sub_id}   logs sub: {logs_sub_id}")
        
        ack = json.loads(await ws.recv())
        if "error" in ack:
            raise RuntimeError(f"ws fail: {ack}")
        sub_id = ack["result"]
        print(f"subscribed: {sub_id}")
        
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "eth_subscription":
                continue
            _print_log(msg["params"]["result"])