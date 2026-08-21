# decoders for the orderbook events of the deployed crystal contracts.
#
# OrdersUpdated is emitted as a raw log3 (sig, market, user) whose data is one
# abi bytes payload of packed entries: flag<<252 | price<<168 | orderId<<112 |
# size, per CrystalMarket.sol's _addToOrdersUpdatedEvent call sites. the entry
# layout was verified against a real mainnet log (tx 0x74ee7c9091d6969fd601c879
# 3f460282c1fe635f8e0d3fe9395df07312b5924d, block 97754330). the fill decode was
# validated against 239 real mainnet fills on 2026-08-20: price, maker and side
# all agree with the resting order's add at fill time, and the amount halves
# reconcile with the same tx's Trade event (tx 0x6e38463447a9b5c87e1e11ccf7ed42
# f106041aa4aac99fa125f0f0670bd19754: amount_out equals Trade.amountOut exactly,
# amount_high equals Trade.amountIn net of the taker fee)

from __future__ import annotations

FILL_TOPIC = "0xa195980963150be5fcca4acd6a80bf5a9de7f9c862258501b7c705e7d2c2d2f4"
ORDERS_UPDATED_TOPIC = "0x7ebb55d14fb18179d0ee498ab0f21c070fad7368e44487d51cdac53d6f74812c"

_U128 = (1 << 128) - 1
_U112 = (1 << 112) - 1
_U80 = (1 << 80) - 1
_U56 = (1 << 56) - 1

# flag nibble: 0/1 buy/sell removal (cancelled or fully consumed), 2/3 buy/sell
# add, 4/5 buy/sell size decrease where the size field carries the decrement
ACTIONS = {0: "remove", 1: "remove", 2: "add", 3: "add", 4: "decrease", 5: "decrease"}


# hex event data into 32 byte words, tolerant of a leading 0x
def _words(data: str) -> list[int]:
    d = data or ""
    if d.startswith("0x"):
        d = d[2:]
    return [int(d[i * 64 : (i + 1) * 64], 16) for i in range(len(d) // 64)]


# one packed order entry, none when the flag nibble is not a known action
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


# OrdersUpdated raw log3: market and user in topics, abi bytes of entries in data
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


# Fill(market indexed, maker indexed, fillInfo, fillAmount): fillInfo shares the
# entry packing with the taker's side in the flag nibble (0 = taker bought, so
# the maker order was a sell) and the maker order's remaining size in the low
# bits. fillAmount halves: high = taker input net of the taker fee, low = taker
# output, both confirmed against the paired Trade event on mainnet
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


# UserRegistered(bool indexed isMargin, address indexed user, uint256 indexed
# userId): the on-chain user registry. client order ids embed the user id as
# their low 41 bits, so this is how orders resolve to owning wallets
USER_REGISTERED_TOPIC = "0xc9c1b51eb96995e1cfea90cc81876d702d5d0a6bf11011d9963fd4d96886f102"


# decode a userregistered log into the id and its owning wallet
def parse_user_registered(addr: str, topics: list[str], data: str) -> dict | None:
    if len(topics) < 4:
        return None
    return {
        "is_margin": int(topics[1], 16) != 0,
        "user": "0x" + topics[2][-40:].lower(),
        "user_id": int(topics[3], 16),
    }
