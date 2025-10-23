from __future__ import annotations

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

def chunks(s: str, n: int):
    return (s[i:i+n] for i in range(0, len(s), n))

def parse_trade(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]).lower() if len(tops) > 1 else addr.lower()
    user = to_addr(tops[2]).lower() if len(tops) > 2 else ""

    words = list(chunks(data_no0x, 64))
    
    def u(i: int, d: int = 0) -> int:
        return int(words[i], 16) if i < len(words) else d

    is_buy = (u(0) != 0)
    amount_in = u(1)
    amount_out = u(2)
    start_px = u(3)
    end_px = u(4)

    return {
        "market": market,
        "user": user,
        "is_buy": is_buy,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "start_price": start_px,
        "end_price": end_px,
    }

def parse_vault_created(addr: str, tops: list[str], data_no0x: str) -> dict:
    vault = to_addr(tops[1]).lower() if len(tops) > 1 else addr.lower()
    words = list(chunks(data_no0x, 64))
    
    def uaddr(i: int) -> str:
        return ("0x" + words[i][-40:]).lower() if i < len(words) else "0x0000000000000000000000000000000000000000"
    
    quote = uaddr(0)
    base = uaddr(1)
    
    return {
        "vault": vault, 
        "quote": quote, 
        "base": base
    }