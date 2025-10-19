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
    amount_out = int(words[2], 16) if len(words) > 2 else 0
    native_reserve = int(words[3], 16) if len(words) > 3 else 0
    token_reserve = int(words[4], 16) if len(words) > 4 else 0

    return {
        "token": token,
        "user": user,
        "is_buy": is_buy,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "native_reserve": native_reserve,
        "token_reserve": token_reserve,
    }

def parse_token_created(addr, tops, data):
    token = to_addr(tops[1]).lower()
    creator = to_addr(tops[2]).lower()
    return { "token": token, "creator": creator }
