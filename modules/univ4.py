from __future__ import annotations

V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"


def _words(data_no0x: str) -> list[str]:
    return [data_no0x[i : i + 64] for i in range(0, len(data_no0x) - len(data_no0x) % 64, 64)]


def _signed(word: str) -> int:
    if not word:
        return 0
    try:
        return int.from_bytes(bytes.fromhex(word), "big", signed=True)
    except ValueError:
        return 0


def _unsigned(word: str) -> int:
    if not word:
        return 0
    try:
        return int(word, 16)
    except ValueError:
        return 0


def _to_addr(topic_or_word: str) -> str:
    raw = topic_or_word[2:] if topic_or_word.startswith("0x") else topic_or_word
    if len(raw) < 40:
        return ""
    return ("0x" + raw[-40:]).lower()


def _pool_id(topic: str) -> str:
    raw = topic[2:] if topic.startswith("0x") else topic
    return ("0x" + raw).lower() if len(raw) == 64 else ""


def parse_v4_initialize(_addr: str, topics: list[str], data_no0x: str) -> dict | None:
    if len(topics) < 4:
        return None

    pool_id = _pool_id(str(topics[1]))
    if not pool_id:
        return None

    w = _words(data_no0x)
    return {
        "pool_id": pool_id,
        "currency0": _to_addr(str(topics[2])),
        "currency1": _to_addr(str(topics[3])),
        "fee": _unsigned(w[0]) if len(w) > 0 else 0,
        "tick_spacing": _signed(w[1]) if len(w) > 1 else 0,
        "hooks": _to_addr(w[2]) if len(w) > 2 else "",
        "sqrt_price_x96": _unsigned(w[3]) if len(w) > 3 else 0,
        "tick": _signed(w[4]) if len(w) > 4 else 0,
    }


def parse_v4_swap(_addr: str, topics: list[str], data_no0x: str) -> dict | None:
    if len(topics) < 3:
        return None

    pool_id = _pool_id(str(topics[1]))
    if not pool_id:
        return None

    w = _words(data_no0x)
    if len(w) < 6:
        return None

    return {
        "pool_id": pool_id,
        "sender": _to_addr(str(topics[2])),
        "amount0": _signed(w[0]),
        "amount1": _signed(w[1]),
        "sqrt_price_x96": _unsigned(w[2]),
        "liquidity": _unsigned(w[3]),
        "tick": _signed(w[4]),
        "fee": _unsigned(w[5]),
    }
