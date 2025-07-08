import json, decimal, asyncio, time
from decimal import Decimal

decimal.getcontext().prec = 50


WS_URL = "wss://testnet-rpc.monad.xyz"
CONTRACTS = {
    "MONUSDC": "0xCd5455B24f3622A1CfEce944615AE5Bc8f36Ee18",
    "sMONMON": "0x97fa0031E2C9a21F0727bcaB884E15c090eC3ee3",
    "aprMONMON": "0x33C5Dc9091952870BD1fF47c89fA53D63f9729b6",
    "shMONMON": "0xcB5ec6D6d0E49478119525E4013ff333Fc46B742",
    "DAKMON": "0x93cBC4b52358c489665680182f0056f4F23C76CD",
    "CHOGMON": "0xf00A3bd942DC0e32d07048ED6255E281667784f6",
    "YAKIMON": "0x3051ec9feFaEc14F2bAB836FAb5A4c970A71874a",
    "WETHUSDC": "0x9fA48CFB43829A932A227e4d7996e310ccf40E9C",
    "WBTCUSDC": "0x45f7db719367bbf9E508D3CeA401EBC62fc732a9",
    "WSOLUSDC": "0x5a6f296032AaAE6737ed5896bc09d01dc2d42507",
    "USDTUSDC": "0xCF16582dC82c4C17fA5b54966ee67b74FD715fB5",
    "ROUTER": "0x4e77071D619Aa164cA6427547aefA41AC51BE7A0",
}
ADDRS = [a.lower() for a in CONTRACTS.values()]
MARKETS = {
    "0xcd5455b24f3622a1cfece944615ae5bc8f36ee18": (10**15, 1000, 6, 18),
    "0x97fa0031e2c9a21f0727bcab884e15c090ec3ee3": (10**4, 10000, 18, 18),
    "0x33c5dc9091952870bd1ff47c89fa53d63f9729b6": (10**4, 10000, 18, 18),
    "0xcb5ec6d6d0e49478119525e4013ff333fc46b742": (10**4, 10000, 18, 18),
    "0x93cbc4b52358c489665680182f0056f4f23c76cd": (10**4, 10000, 18, 18),
    "0xf00a3bd942dc0e32d07048ed6255e281667784f6": (10**5, 100000, 18, 18),
    "0x3051ec9fefaec14f2bab836fab5a4c970a71874a": (10**6, 1000000, 18, 18),
    "0x9fa48cfb43829a932a227e4d7996e310ccf40e9c": (10**13, 10, 6, 18),
    "0x45f7db719367bbf9e508d3cea401ebc62fc732a9": (10**2, 1, 6, 8),
    "0x5a6f296032aaae6737ed5896bc09d01dc2d42507": (10**5, 100, 6, 9),
    "0xcf16582dc82c4c17fa5b54966ee67b74fd715fb5": (10**3, 1000, 6, 6),
}
EVENT_SIGS = {
    "0xc3bcf95b5242764f3f2dc3e504ce05823a3b50c4ccef5e660d13beab2f51f2ca": "OF",
    "0x1c87843c023cd30242ff04316b77102e873496e3d8924ef015475cf066c1d4f4": "OU",
    "0xa4536bf9ecc2bb029e4c6500446c0543db158200d0041507373066024d5d2099": "UU",
    "0x9d05414fb79fac216c15606de5cc06664e91a254e4d5f57664d5f1beaf7fb7ef": "RA",
}
TOPICS = list(EVENT_SIGS.keys())


def scale_price(raw: int, addr: str) -> Decimal:
    _, pf, _, _ = MARKETS[addr]
    return Decimal(raw) / Decimal(pf)


def scale_size_quote(raw: int, addr: str) -> Decimal:
    sf, _, qd, _ = MARKETS[addr]
    return Decimal(raw) / (Decimal(sf) * Decimal(10**qd))


def scale_fill_quote(raw: int, addr: str) -> Decimal:
    _, _, qd, _ = MARKETS[addr]
    return Decimal(raw) / Decimal(10**qd)


def scale_fill_base(raw: int, addr: str) -> Decimal:
    _, _, _, bd = MARKETS[addr]
    return Decimal(raw) / Decimal(10**bd)


def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]


def chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))


def parse_referral(t, d):
    return {"referrer": to_addr(t[1]), "referee": "0x" + d[-40:]}


def parse_username(topics, data):
    caller = to_addr(topics[1])
    length = int(data[64:128], 16)
    name_hex = data[128 : 128 + length * 2]
    return {
        "caller": caller,
        "username": bytes.fromhex(name_hex).decode("utf-8", "replace"),
    }


def parse_orders_filled(addr, tops, data):
    caller = to_addr(tops[1])
    amount_in_raw = int(data[0:32], 16)
    amount_out_raw = int(data[32:64], 16)
    buy_sell_flag = int(data[64], 16) & 1
    is_buy = buy_sell_flag == 1
    start_price_raw = int(data[65:96], 16)
    end_price_raw = int(data[96:128], 16)
    offset_bytes = int(data[128:192], 16)
    filled_len = int(data[offset_bytes * 2 : offset_bytes * 2 + 64], 16)
    filled_start = offset_bytes * 2 + 64
    filled_hex = data[filled_start : filled_start + filled_len * 2]

    amount_in = (
        scale_fill_quote(amount_in_raw, addr)
        if is_buy
        else scale_fill_base(amount_in_raw, addr)
    )
    amount_out = (
        scale_fill_quote(amount_out_raw, addr)
        if not is_buy
        else scale_fill_base(amount_out_raw, addr)
    )
    start_price = scale_price(start_price_raw, addr)
    end_price = scale_price(end_price_raw, addr)

    fills = []
    for seg in chunks(filled_hex, 64):
        if len(seg) < 64:
            continue
        price_raw = int(seg[1:20], 16)
        suffix = int(seg[20:32], 16)
        new_raw = int(seg[32:64], 16)
        fills.append(
            {
                "price": scale_price(price_raw, addr),
                "order_id": f"{price_raw}-{addr}-{suffix}",
                "new_size": scale_size_quote(new_raw, addr),
            }
        )

    return {
        "caller": caller,
        "buySell": buy_sell_flag,
        "side": "BUY" if is_buy else "SELL",
        "amount_in": amount_in,
        "amount_out": amount_out,
        "start_price": start_price,
        "end_price": end_price,
        "fills": fills,
    }


def parse_orders_updated(addr, tops, data):
    caller = to_addr(tops[1])
    ln = int(data[64:128], 16)
    body = data[128 : 128 + ln * 2]
    ops = []
    for seg in chunks(body, 64):
        if len(seg) < 64:
            continue
        flag = int(seg[0], 16)
        side = "BUY" if flag & 1 else "SELL"
        price_raw = int(seg[1:20], 16)
        suffix = int(seg[20:32], 16)
        q_raw = int(seg[32:64], 16)
        ops.append(
            {
                "action": "PLACE" if flag < 2 else "CANCEL",
                "side": side,
                "price": scale_price(price_raw, addr),
                "order_id": f"{price_raw}-{addr}-{suffix}",
                "quote_size": scale_size_quote(q_raw, addr),
            }
        )
    return {"caller": caller, "ops": ops}


def _parse_ra(_addr, topics, data):
    return parse_referral(topics, data)


def _parse_uu(_addr, topics, data):
    return parse_username(topics, data)


PARSERS = {
    "RA": _parse_ra,
    "UU": _parse_uu,
    "OF": parse_orders_filled,
    "OU": parse_orders_updated,
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