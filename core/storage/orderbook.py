# writes and reads for the decoded orderbook plane. every applier is replay safe:
# the events and fills primary keys are the guard, and a row that fails to insert
# has already been applied, so its state mutation is skipped. state mutations are
# also monotonic in block number, because the history sweep replays old blocks
# while the live indexer is already ahead: a historical event must land in the
# events table without dragging the order row back to a stale price or size

from __future__ import annotations

from typing import Any

from psycopg2.extras import execute_values

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

        _apply_order_entry(o, market, user, blk, blk_ts, cur)


# the state mutation for one fresh event entry, shared by the live applier and
# the batched sweep path so there is exactly one book-evolution implementation
def _apply_order_entry(o: dict, market: str, user: str, blk: int, blk_ts: int, cur) -> None:
    if o["action"] == "add":
        cur.execute(
            """
            INSERT INTO crystal_orderbook_orders
                (market, price, order_id, user_address, is_buy, size, status,
                 created_block, created_ts, updated_block, updated_ts)
            VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s)
            ON CONFLICT (market, price, order_id) DO UPDATE SET
                user_address = EXCLUDED.user_address,
                is_buy = EXCLUDED.is_buy,
                size = EXCLUDED.size,
                status = 'open',
                updated_block = EXCLUDED.updated_block,
                updated_ts = EXCLUDED.updated_ts
            WHERE crystal_orderbook_orders.updated_block <= EXCLUDED.updated_block
            """,
            (
                market,
                int(o["price"]),
                int(o["order_id"]),
                user,
                bool(o["is_buy"]),
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
                (market, price, order_id, user_address, is_buy, size, status,
                 created_block, created_ts, updated_block, updated_ts)
            VALUES (%s, %s, %s, %s, %s, 0, 'removed', %s, %s, %s, %s)
            ON CONFLICT (market, price, order_id) DO UPDATE SET
                status = 'removed',
                size = 0,
                updated_block = EXCLUDED.updated_block,
                updated_ts = EXCLUDED.updated_ts
            WHERE crystal_orderbook_orders.updated_block <= EXCLUDED.updated_block
            """,
            (market, int(o["price"]), int(o["order_id"]), user, bool(o["is_buy"]), blk, blk_ts, blk, blk_ts),
        )
    elif o["action"] == "decrease":
        cur.execute(
            """
            UPDATE crystal_orderbook_orders
            SET size = GREATEST(size - %s, 0), updated_block = %s, updated_ts = %s
            WHERE market = %s AND price = %s AND order_id = %s AND updated_block <= %s
            """,
            (int(o["size"]), blk, blk_ts, market, int(o["price"]), int(o["order_id"]), int(blk)),
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

    _apply_fill_mutation(parsed, market, blk, blk_ts, cur)


# the order-row consequence of one fresh fill, shared with the batched sweep path
def _apply_fill_mutation(parsed: dict, market: str, blk: int, blk_ts: int, cur) -> None:
    remaining = int(parsed["remaining"])
    cur.execute(
        """
        UPDATE crystal_orderbook_orders
        SET size = %s,
            status = CASE WHEN %s = 0 THEN 'removed' ELSE status END,
            updated_block = %s, updated_ts = %s
        WHERE market = %s AND price = %s AND order_id = %s AND updated_block <= %s
        """,
        (remaining, remaining, blk, blk_ts, market, int(parsed["price"]), int(parsed["order_id"]), int(blk)),
    )


# current updated_block per order key, the sweep's client-side prefilter: a row
# the live indexer already moved past this whole range needs no mutation calls
def get_order_updated_blocks(keys: list[tuple[str, int, int]], cur) -> dict[tuple[str, int, int], int]:
    if not keys:
        return {}
    cur.execute(
        """
        SELECT market, price, order_id, updated_block FROM crystal_orderbook_orders
        WHERE (market, price, order_id) IN %s
        """,
        (tuple(keys),),
    )
    return {(m, int(p), int(oid)): int(ub) for m, p, oid, ub in cur.fetchall()}


# insert many event entries in one statement, returning the keys that were new.
# rows: (txhash, log_index, entry_index, block_number, timestamp, market,
#        user_address, flag, is_buy, action, price, order_id, size)
def batch_insert_orderbook_events(rows: list[tuple], cur) -> set[tuple[str, int, int]]:
    if not rows:
        return set()
    fresh = execute_values(
        cur,
        """
        INSERT INTO crystal_orderbook_events
            (txhash, log_index, entry_index, block_number, timestamp, market,
             user_address, flag, is_buy, action, price, order_id, size)
        VALUES %s
        ON CONFLICT (txhash, log_index, entry_index) DO NOTHING
        RETURNING txhash, log_index, entry_index
        """,
        rows,
        fetch=True,
    )
    return {(tx, int(li), int(ei)) for tx, li, ei in fresh}


# insert many fills in one statement, returning the keys that were new.
# rows: (txhash, log_index, block_number, timestamp, market, maker,
#        maker_is_buy, price, order_id, remaining, amount_high, amount_out)
def batch_insert_orderbook_fills(rows: list[tuple], cur) -> set[tuple[str, int]]:
    if not rows:
        return set()
    fresh = execute_values(
        cur,
        """
        INSERT INTO crystal_orderbook_fills
            (txhash, log_index, block_number, timestamp, market, maker,
             maker_is_buy, price, order_id, remaining, amount_high, amount_out)
        VALUES %s
        ON CONFLICT (txhash, log_index) DO NOTHING
        RETURNING txhash, log_index
        """,
        rows,
        fetch=True,
    )
    return {(tx, int(li)) for tx, li in fresh}


# record one on-chain user registration, replay safe on the id
def insert_crystal_user(parsed: dict, blk: int, blk_ts: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as c:
            insert_crystal_user(parsed, blk, blk_ts, cur=c)
        return
    cur.execute(
        """
        INSERT INTO crystal_users (user_id, user_address, is_margin, block_number, timestamp)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (int(parsed["user_id"]), (parsed.get("user") or "").lower(), bool(parsed.get("is_margin")), int(blk), int(blk_ts)),
    )


# the registered user ids for one wallet
def list_user_ids(user: str) -> list[int]:
    with db_cursor() as cur:
        cur.execute("SELECT user_id FROM crystal_users WHERE user_address = %s ORDER BY user_id", ((user or "").lower(),))
        return [int(r[0]) for r in cur.fetchall()]


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
            # client ids arrive on chain as (cloid << 41 | user_id), native
            # per-level ids stay below that range
            "cloid": int(oid) >> 41 if int(oid) >> 41 else None,
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


# one taker trade row, the primary key making replays a no-op
def insert_market_trade(parsed: dict, blk: int, blk_ts: int, txhash: str, log_index: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as c:
            insert_market_trade(parsed, blk, blk_ts, txhash, log_index, cur=c)
        return
    cur.execute(
        """
        INSERT INTO crystal_market_trades
            (txhash, log_index, block_number, timestamp, market, user_address,
             is_buy, amount_in, amount_out, start_price, end_price)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING
        """,
        (
            (txhash or "").lower(),
            int(log_index),
            int(blk),
            int(blk_ts),
            (parsed.get("market") or "").lower(),
            (parsed.get("user") or "").lower(),
            bool(parsed.get("is_buy")),
            int(parsed.get("amount_in") or 0),
            int(parsed.get("amount_out") or 0),
            int(parsed.get("start_price") or 0),
            int(parsed.get("end_price") or 0),
        ),
    )


# the full exchange trade history for one wallet: taker trades and maker fills
# merged newest first, so ordercenter shows both sides of every execution
def list_exchange_trades(
    user: str, market: str | None = None, limit: int = 100, before_ts: int | None = None
) -> list[dict[str, Any]]:
    addr = (user or "").lower()
    mkt = (market or "").lower() if market else None
    cutoff = int(before_ts) if before_ts is not None else None

    taker_where = "user_address = %(u)s"
    maker_where = "maker = %(u)s"
    params: dict[str, Any] = {"u": addr, "lim": int(limit)}
    if mkt:
        taker_where += " AND market = %(m)s"
        maker_where += " AND market = %(m)s"
        params["m"] = mkt
    if cutoff is not None:
        taker_where += " AND timestamp < %(cut)s"
        maker_where += " AND timestamp < %(cut)s"
        params["cut"] = cutoff

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT 'taker' AS side_kind, txhash, log_index, block_number, timestamp,
                       market, is_buy, amount_in, amount_out, start_price, end_price,
                       NULL::bigint AS order_id
                FROM crystal_market_trades WHERE {taker_where}
                UNION ALL
                SELECT 'maker' AS side_kind, txhash, log_index, block_number, timestamp,
                       market, maker_is_buy AS is_buy, amount_high AS amount_in,
                       amount_out, price AS start_price, price AS end_price, order_id
                FROM crystal_orderbook_fills WHERE {maker_where}
            ) u
            ORDER BY timestamp DESC, log_index DESC
            LIMIT %(lim)s
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {
            "kind": k,
            "txhash": tx,
            "log_index": int(li),
            "block_number": int(bn),
            "timestamp": int(ts),
            "market": m,
            "is_buy": bool(b),
            "amount_in": str(int(ai or 0)),
            "amount_out": str(int(ao or 0)),
            "start_price": str(int(sp or 0)),
            "end_price": str(int(ep or 0)),
            "order_id": int(oid) if oid is not None else None,
        }
        for k, tx, li, bn, ts, m, b, ai, ao, sp, ep, oid in rows
    ]


# order lifecycle history for one wallet: places, cancels, decreases and the
# fills that consumed them, newest first
def list_order_history(
    user: str, market: str | None = None, limit: int = 100, before_ts: int | None = None
) -> list[dict[str, Any]]:
    addr = (user or "").lower()
    mkt = (market or "").lower() if market else None
    cutoff = int(before_ts) if before_ts is not None else None

    ev_where = "user_address = %(u)s"
    fill_where = "maker = %(u)s"
    params: dict[str, Any] = {"u": addr, "lim": int(limit)}
    if mkt:
        ev_where += " AND market = %(m)s"
        fill_where += " AND market = %(m)s"
        params["m"] = mkt
    if cutoff is not None:
        ev_where += " AND timestamp < %(cut)s"
        fill_where += " AND timestamp < %(cut)s"
        params["cut"] = cutoff

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT action, txhash, log_index, entry_index, block_number, timestamp,
                       market, is_buy, price, order_id, size
                FROM crystal_orderbook_events WHERE {ev_where}
                UNION ALL
                SELECT 'fill' AS action, txhash, log_index, 0 AS entry_index, block_number,
                       timestamp, market, maker_is_buy AS is_buy, price, order_id,
                       amount_out AS size
                FROM crystal_orderbook_fills WHERE {fill_where}
            ) u
            ORDER BY timestamp DESC, log_index DESC, entry_index DESC
            LIMIT %(lim)s
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {
            "action": a,
            "txhash": tx,
            "log_index": int(li),
            "entry_index": int(ei),
            "block_number": int(bn),
            "timestamp": int(ts),
            "market": m,
            "is_buy": bool(b),
            "price": str(int(p or 0)),
            "order_id": int(oid),
            "size": str(int(sz or 0)),
        }
        for a, tx, li, ei, bn, ts, m, b, p, oid, sz in rows
    ]
