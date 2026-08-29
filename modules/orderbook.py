from __future__ import annotations

FILL_TOPIC = "0xa195980963150be5fcca4acd6a80bf5a9de7f9c862258501b7c705e7d2c2d2f4"
ORDERS_UPDATED_TOPIC = "0x7ebb55d14fb18179d0ee498ab0f21c070fad7368e44487d51cdac53d6f74812c"

_U128 = (1 << 128) - 1
_U112 = (1 << 112) - 1
_U80 = (1 << 80) - 1
_U56 = (1 << 56) - 1

ACTIONS = {0: "remove", 1: "remove", 2: "add", 3: "add", 4: "decrease", 5: "decrease"}


def _words(data: str) -> list[int]:
    d = data or ""
    if d.startswith("0x"):
        d = d[2:]
    return [int(d[i * 64 : (i + 1) * 64], 16) for i in range(len(d) // 64)]


def _entry(word: int) -> dict | None:
    flag = word >> 252
    action = ACTIONS.get(flag)
    if action is None:
        return None
    return {
        "flag": flag,
        "action": action,
        "is_buy": flag % 2 == 0,
        "price": (word >> 168) & _U80,
        "order_id": (word >> 112) & _U56,
        "size": word & _U112,
    }


def parse_orders_updated(addr: str, topics: list[str], data: str) -> dict | None:
    if len(topics) < 3:
        return None
    words = _words(data)
    if len(words) < 2:
        return None
    n = min(words[1] // 32, max(len(words) - 2, 0))
    orders = []
    for w in words[2 : 2 + n]:
        e = _entry(w)
        if e is not None:
            orders.append(e)
    return {
        "market": "0x" + topics[1][-40:].lower(),
        "user": "0x" + topics[2][-40:].lower(),
        "orders": orders,
    }


def parse_fill(addr: str, topics: list[str], data: str) -> dict | None:
    if len(topics) < 3:
        return None
    words = _words(data)
    if len(words) < 2:
        return None
    info, amount = words[0], words[1]
    flag = info >> 252
    return {
        "market": "0x" + topics[1][-40:].lower(),
        "maker": "0x" + topics[2][-40:].lower(),
        "flag": flag,
        "maker_is_buy": flag == 1,
        "price": (info >> 168) & _U80,
        "order_id": (info >> 112) & _U56,
        "remaining": info & _U112,
        "amount_high": amount >> 128,
        "amount_out": amount & _U128,
    }


USER_REGISTERED_TOPIC = "0xc9c1b51eb96995e1cfea90cc81876d702d5d0a6bf11011d9963fd4d96886f102"
USER_REGISTERED_V2_TOPIC = "0xe29d35093005f4d575e1003753426b57a7f64378ba73332eef9c6ccc2b8decd6"


def parse_user_registered(addr: str, topics: list[str], data: str) -> dict | None:
    if topics and str(topics[0]).lower() == USER_REGISTERED_V2_TOPIC:
        if len(topics) < 3:
            return None
        return {
            "is_margin": False,
            "user": "0x" + topics[1][-40:].lower(),
            "user_id": int(topics[2], 16),
        }
    if len(topics) < 4:
        return None
    return {
        "is_margin": int(topics[1], 16) != 0,
        "user": "0x" + topics[2][-40:].lower(),
        "user_id": int(topics[3], 16),
    }
