# decoders for the orderbook's raw log2 events. these two topics are emitted via
# assembly rather than emit, so generic abi tooling never sees them, which is why
# batch placed orders were invisible and remaining sizes went stale.
#
# layouts read directly from contract.sol's emission assembly. no real fill exists
# on the current deployment yet, so the vectors in tests are synthetic: the first
# real exchange trade must be checked against these decoders before they are
# trusted for balances or order state

from __future__ import annotations

FILL_TOPIC = "0xc3bcf95b5242764f3f2dc3e504ce05823a3b50c4ccef5e660d13beab2f51f2ca"
BATCH_ORDERS_TOPIC = "0x1c87843c023cd30242ff04316b77102e873496e3d8924ef015475cf066c1d4f4"

_U128 = (1 << 128) - 1
_U80 = (1 << 80) - 1
_U48 = (1 << 48) - 1


# one packed resting-order entry: price<<176 | orderId<<128 | size
def _entry(word: int) -> dict:
    return {"price": word >> 176 & _U80, "order_id": word >> 128 & _U48, "size": word & _U128}


# OrdersFilled raw log: abi words (amounts, info, offset, len, entries...). the
# entries carry each touched resting order's REMAINING size, zero when fully filled
def decode_fill(topics: list[str], data: str) -> dict | None:
    if not topics or topics[0].lower() != FILL_TOPIC:
        return None
    d = (data or "")[2:]
    words = [int(d[i * 64 : (i + 1) * 64], 16) for i in range(len(d) // 64)]
    if len(words) < 4:
        return None
    amounts, info, _, blen = words[0], words[1], words[2], words[3]
    n = blen // 32
    entries = [_entry(w) for w in words[4 : 4 + n]]
    return {
        "caller": "0x" + topics[1][-40:].lower(),
        "amount_in": amounts >> 128,
        "amount_out": amounts & _U128,
        "is_buy": bool(info >> 252 & 1),
        "first_fill_price": info >> 128 & _U80,
        "last_fill_price": info & _U128,
        "touched": entries,
    }


# OrdersUpdated, both the emit form and the batch raw form share entry packing:
# flag(word>>252): 0 sell-add, 1 buy-add, 2 sell-cancel, 3 buy-cancel
def decode_order_updates(topics: list[str], data: str) -> dict | None:
    if not topics or topics[0].lower() != BATCH_ORDERS_TOPIC:
        return None
    d = (data or "")[2:]
    words = [int(d[i * 64 : (i + 1) * 64], 16) for i in range(len(d) // 64)]
    if len(words) < 2:
        return None
    blen = words[1]
    n = blen // 32
    out = []
    for w in words[2 : 2 + n]:
        flag = w >> 252
        e = _entry(w & ((1 << 252) - 1))
        e["is_buy"] = flag in (1, 3)
        e["is_cancel"] = flag in (2, 3)
        out.append(e)
    return {"owner": "0x" + topics[1][-40:].lower(), "orders": out}
