from __future__ import annotations

from typing import Iterable


_ZERO_ADDR = "0x" + "0" * 40


def to_addr(word) -> str:
    return "0x" + (word.hex() if isinstance(word, bytes) else str(word))[-40:]


def _chunks(s: str, n: int) -> Iterable[str]:
    return (s[i:i + n] for i in range(0, len(s or ""), n))


def _u(words: list[str], i: int, default: int = 0) -> int:
    try:
        return int(words[i], 16) if i < len(words) else default
    except Exception:
        return default


def parse_sync(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]).lower() if len(tops) > 1 else _ZERO_ADDR
    words = list(_chunks(data_no0x, 64))
    return {
        "market": market,
        "reserveQuote": _u(words, 0),
        "reserveBase": _u(words, 1),
    }


def parse_mint(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]).lower() if len(tops) > 1 else _ZERO_ADDR
    sender = to_addr(tops[2]).lower() if len(tops) > 2 else _ZERO_ADDR
    words = list(_chunks(data_no0x, 64))
    return {
        "market": market,
        "sender": sender,
        "amountQuote": _u(words, 0),
        "amountBase": _u(words, 1),
    }


def parse_burn(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]).lower() if len(tops) > 1 else _ZERO_ADDR
    sender = to_addr(tops[2]).lower() if len(tops) > 2 else _ZERO_ADDR
    to = to_addr(tops[3]).lower() if len(tops) > 3 else _ZERO_ADDR
    words = list(_chunks(data_no0x, 64))
    return {
        "market": market,
        "sender": sender,
        "to": to,
        "amountQuote": _u(words, 0),
        "amountBase": _u(words, 1),
    }
