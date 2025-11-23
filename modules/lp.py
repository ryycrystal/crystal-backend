from __future__ import annotations

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

def chunks(s: str, n: int):
    return (s[i:i+n] for i in range(0, len(s), n))

def parse_sync(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]) if len(tops) > 1 else ""
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    words = list(chunks(h, 64))
    reserve_quote = int(words[0], 16) if len(words) > 0 else 0
    reserve_base = int(words[1], 16) if len(words) > 1 else 0
    return {
        "market": market.lower(),
        "reserveQuote": reserve_quote,
        "reserveBase": reserve_base,
    }