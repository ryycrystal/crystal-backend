# writes and reads for the decoded orderbook plane. every applier is replay safe:
# the events and fills primary keys are the guard, and a row that fails to insert
# has already been applied, so its state mutation is skipped

from __future__ import annotations

from typing import Any

from .base import db_cursor


# apply one orders-updated log: record each entry and evolve the order it touches
def apply_orderbook_updates(parsed: dict, blk: int, blk_ts: int, txhash: str, log_index: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as c:
            apply_orderbook_updates(parsed, blk, blk_ts, txhash, log_index, cur=c)
        return

    market = (parsed.get("market") or "").lower()
    user = (parsed.get("user") or "").lower()
    txhash = (txhash or "").lower()

    for i, o in enumerate(parsed.get("orders") or []):
        cur.execute(
            """
            INSERT INTO crystal_orderbook_events
                (txhash, log_index, entry_index, block_number, timestamp, market,
                 user_address, flag, is_buy, action, price, order_id, size)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (txhash, log_index, entry_index) DO NOTHING
            """,
            (
                txhash,
                int(log_index),
                i,
                int(blk),
                int(blk_ts),
                market,
                user,
                int(o["flag"]),
                bool(o["is_buy"]),
                o["action"],
                int(o["price"]),
                int(o["order_id"]),
                int(o["size"]),
            ),
        )
        if cur.rowcount == 0:
            continue

        if o["action"] == "add":
            cur.execute(
                """
                INSERT INTO crystal_orderbook_orders
                    (market, order_id, user_address, is_buy, price, size, status,
                     created_block, created_ts, updated_block, updated_ts)
                VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s)
                ON CONFLICT (market, order_id) DO UPDATE SET
                    user_address = EXCLUDED.user_address,
                    is_buy = EXCLUDED.is_buy,
                    price = EXCLUDED.price,
                    size = EXCLUDED.size,
                    status = 'open',
                    updated_block = EXCLUDED.updated_block,
                    updated_ts = EXCLUDED.updated_ts
                """,
                (
                    market,
                    int(o["order_id"]),
                    user,
                    bool(o["is_buy"]),
                    int(o["price"]),
                    int(o["size"]),
                    blk,
                    blk_ts,
                    blk,
                    blk_ts,
                ),
            )
        elif o["action"] == "remove":
            cur.execute(
                """
                INSERT INTO crystal_orderbook_orders
                    (market, order_id, user_address, is_buy, price, size, status,
                     created_block, created_ts, updated_block, updated_ts)
                VALUES (%s, %s, %s, %s, %s, 0, 'removed', %s, %s, %s, %s)
                ON CONFLICT (market, order_id) DO UPDATE SET
                    status = 'removed',
                    size = 0,
                    updated_block = EXCLUDED.updated_block,
                    updated_ts = EXCLUDED.updated_ts
                """,
                (market, int(o["order_id"]), user, bool(o["is_buy"]), int(o["price"]), blk, blk_ts, blk, blk_ts),
            )
        elif o["action"] == "decrease":
            cur.execute(
                """
                UPDATE crystal_orderbook_orders
                SET size = GREATEST(size - %s, 0), updated_block = %s, updated_ts = %s
                WHERE market = %s AND order_id = %s
                """,
                (int(o["size"]), blk, blk_ts, market, int(o["order_id"])),
            )


# apply one maker fill: record it and move the touched order to its remaining size
def apply_orderbook_fill(parsed: dict, blk: int, blk_ts: int, txhash: str, log_index: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as c:
            apply_orderbook_fill(parsed, blk, blk_ts, txhash, log_index, cur=c)
        return

    market = (parsed.get("market") or "").lower()
    maker = (parsed.get("maker") or "").lower()
    txhash = (txhash or "").lower()

    cur.execute(
        """
        INSERT INTO crystal_orderbook_fills
            (txhash, log_index, block_number, timestamp, market, maker,
             maker_is_buy, price, order_id, remaining, amount_high, amount_out)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING
        """,
        (
            txhash,
            int(log_index),
            int(blk),
            int(blk_ts),
            market,
            maker,
            bool(parsed["maker_is_buy"]),
            int(parsed["price"]),
            int(parsed["order_id"]),
            int(parsed["remaining"]),
            int(parsed["amount_high"]),
            int(parsed["amount_out"]),
        ),
    )
    if cur.rowcount == 0:
        return

    remaining = int(parsed["remaining"])
    cur.execute(
        """
        UPDATE crystal_orderbook_orders
        SET size = %s,
            status = CASE WHEN %s = 0 THEN 'removed' ELSE status END,
            updated_block = %s, updated_ts = %s
        WHERE market = %s AND order_id = %s
        """,
        (remaining, remaining, blk, blk_ts, market, int(parsed["order_id"])),
    )


# open orders for one wallet, newest activity first
def list_open_orders(user: str, market: str | None = None) -> list[dict[str, Any]]:
    where = "user_address = %s AND status = 'open' AND size > 0"
    params: tuple = ((user or "").lower(),)
    if market:
        where += " AND market = %s"
        params = ((user or "").lower(), (market or "").lower())
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT market, order_id, is_buy, price, size, created_block, created_ts, updated_block, updated_ts
            FROM crystal_orderbook_orders
            WHERE {where}
            ORDER BY updated_ts DESC, order_id DESC
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {
            "market": m,
            "order_id": int(oid),
            "is_buy": bool(b),
            "price": str(int(p)),
            "size": str(int(s)),
            "created_block": int(cb),
            "created_ts": int(cts),
            "updated_block": int(ub),
            "updated_ts": int(uts),
        }
        for m, oid, b, p, s, cb, cts, ub, uts in rows
    ]


# decoded order events for one wallet, newest first
def list_orderbook_events(user: str, limit: int = 100) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT txhash, log_index, entry_index, block_number, timestamp, market,
                   flag, is_buy, action, price, order_id, size
            FROM crystal_orderbook_events
            WHERE user_address = %s
            ORDER BY timestamp DESC, log_index DESC, entry_index DESC
            LIMIT %s
            """,
            ((user or "").lower(), int(limit)),
        )
        rows = cur.fetchall()
    return [
        {
            "txhash": tx,
            "log_index": int(li),
            "entry_index": int(ei),
            "block_number": int(bn),
            "timestamp": int(ts),
            "market": m,
            "flag": int(f),
            "is_buy": bool(b),
            "action": a,
            "price": str(int(p)),
            "order_id": int(oid),
            "size": str(int(s)),
        }
        for tx, li, ei, bn, ts, m, f, b, a, p, oid, s in rows
    ]
