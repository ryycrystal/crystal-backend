from __future__ import annotations
from typing import Dict, Any

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

def chunks(s: str, n: int):
    return (s[i:i+n] for i in range(0, len(s), n))

def parse_mint(addr: str, tops: list[str], data_no0x: str) -> Dict[str, Any]:
    market = to_addr(tops[1]) if len(tops) > 1 else ""
    sender = to_addr(tops[2]) if len(tops) > 2 else ""

    h = data_no0x or ""
    if len(h) % 64 != 0:
        h = h.ljust((len(h) // 64 + 1) * 64, "0")

    words = list(chunks(h, 64))
    amount_quote = int(words[0], 16) if len(words) > 0 else 0
    amount_base = int(words[1], 16) if len(words) > 1 else 0

    return {
        "market": market.lower(),
        "sender": sender.lower(),
        "amountQuote": amount_quote,
        "amountBase": amount_base,
    }

def parse_burn(addr: str, tops: list[str], data_no0x: str) -> Dict[str, Any]:
    market = to_addr(tops[1]) if len(tops) > 1 else ""
    sender = to_addr(tops[2]) if len(tops) > 2 else ""
    to_addr_out = to_addr(tops[3]) if len(tops) > 3 else ""

    h = data_no0x or ""
    if len(h) % 64 != 0:
        h = h.ljust((len(h) // 64 + 1) * 64, "0")

    words = list(chunks(h, 64))
    amount_quote = int(words[0], 16) if len(words) > 0 else 0
    amount_base = int(words[1], 16) if len(words) > 1 else 0

    return {
        "market": market.lower(),
        "sender": sender.lower(),
        "to": to_addr_out.lower(),
        "amountQuote": amount_quote,
        "amountBase": amount_base,
    }

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