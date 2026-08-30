from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import execute_values

from .base import db_cursor


def _addr_list(user) -> list[str]:
    if isinstance(user, str):
        return [user.lower()]
    return [str(u).lower() for u in (user or [])]


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


def _apply_order_entry(o: dict, market: str, user: str, blk: int, blk_ts: int, cur) -> None:
    if o["action"] == "add":
        cur.execute(
            """
            INSERT INTO crystal_orderbook_orders
                (market, price, order_id, user_address, is_buy, size, original_size, filled_size, status,
                 created_block, created_ts, updated_block, updated_ts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 'open', %s, %s, %s, %s)
            ON CONFLICT (market, price, order_id) DO UPDATE SET
                user_address = EXCLUDED.user_address,
                is_buy = EXCLUDED.is_buy,
                size = EXCLUDED.size,
                original_size = EXCLUDED.original_size,
                filled_size = 0,
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
            VALUES (%s, %s, %s, %s, %s, 0, 'canceled', %s, %s, %s, %s)
            ON CONFLICT (market, price, order_id) DO UPDATE SET
                status = CASE
                    WHEN crystal_orderbook_orders.status = 'filled' THEN 'filled'
                    ELSE 'canceled'
                END,
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
            SET size = GREATEST(size - %s, 0),
                original_size = GREATEST(original_size - %s, filled_size),
                updated_block = %s, updated_ts = %s
            WHERE market = %s AND price = %s AND order_id = %s AND updated_block <= %s
            """,
            (
                int(o["size"]),
                int(o["size"]),
                blk,
                blk_ts,
                market,
                int(o["price"]),
                int(o["order_id"]),
                int(blk),
            ),
        )


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


def _apply_fill_mutation(parsed: dict, market: str, blk: int, blk_ts: int, cur) -> None:
    remaining = int(parsed["remaining"])
    cur.execute(
        """
        UPDATE crystal_orderbook_orders
        SET filled_size = filled_size + GREATEST(size - %s, 0),
            size = %s,
            status = CASE WHEN %s = 0 THEN 'filled' ELSE status END,
            updated_block = %s, updated_ts = %s
        WHERE market = %s AND price = %s AND order_id = %s AND updated_block <= %s
        """,
        (
            remaining,
            remaining,
            remaining,
            blk,
            blk_ts,
            market,
            int(parsed["price"]),
            int(parsed["order_id"]),
            int(blk),
        ),
    )


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
        (
            int(parsed["user_id"]),
            (parsed.get("user") or "").lower(),
            bool(parsed.get("is_margin")),
            int(blk),
            int(blk_ts),
        ),
    )


def list_user_ids(user: str) -> list[int]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT user_id FROM crystal_users WHERE user_address = %s ORDER BY user_id", ((user or "").lower(),)
        )
        return [int(r[0]) for r in cur.fetchall()]


def list_open_orders(user, market: str | None = None) -> list[dict[str, Any]]:
    where = "o.user_address = ANY(%s) AND o.status = 'open' AND o.size > 0"
    params: tuple = (_addr_list(user),)
    if market:
        where += " AND o.market = %s"
        params = (_addr_list(user), (market or "").lower())
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT o.market, o.order_id, o.is_buy, o.price, o.size, o.original_size, o.filled_size, o.status,
                   o.created_block, o.created_ts, o.updated_block, o.updated_ts, COALESCE(a.txhash, '')
            FROM crystal_orderbook_orders o
            {_ADD_TX_JOIN}
            WHERE {where}
            ORDER BY {_ORDER_SORT}
            """,
            params,
        )
        rows = cur.fetchall()
    return [_order_row(r) for r in rows]


_ORDER_SORT = "o.updated_ts DESC, o.price DESC, o.order_id DESC, o.market DESC"


_ADD_TX_JOIN = """
            LEFT JOIN LATERAL (
                SELECT txhash FROM crystal_orderbook_events e
                WHERE e.market = o.market AND e.price = o.price AND e.order_id = o.order_id
                  AND e.action = 'add'
                ORDER BY e.block_number DESC, e.log_index DESC, e.entry_index DESC
                LIMIT 1
            ) a ON TRUE
"""


def _order_row(r: tuple) -> dict[str, Any]:
    m, oid, b, p, s, orig, fil, st, cb, cts, ub, uts, txh = r
    return {
        "market": m,
        "order_id": int(oid),
        "cloid": int(oid) >> 41 if int(oid) >> 41 else None,
        "is_buy": bool(b),
        "price": str(int(p)),
        "size": str(int(s)),
        "original_size": str(int(orig)),
        "filled_size": str(int(fil)),
        "status": st,
        "txhash": txh,
        "created_block": int(cb),
        "created_ts": int(cts),
        "updated_block": int(ub),
        "updated_ts": int(uts),
    }


def list_wallet_orders(
    user, market: str | None = None, limit: int = 200, before_ts: int | None = None
) -> list[dict[str, Any]]:
    where = "o.user_address = ANY(%(u)s)"
    params: dict[str, Any] = {"u": _addr_list(user), "lim": int(limit)}
    if market:
        where += " AND o.market = %(m)s"
        params["m"] = (market or "").lower()
    if before_ts is not None:
        where += " AND o.updated_ts < %(cut)s"
        params["cut"] = int(before_ts)
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT o.market, o.order_id, o.is_buy, o.price, o.size, o.original_size, o.filled_size, o.status,
                   o.created_block, o.created_ts, o.updated_block, o.updated_ts, COALESCE(a.txhash, '')
            FROM crystal_orderbook_orders o
            {_ADD_TX_JOIN}
            WHERE {where}
            ORDER BY {_ORDER_SORT}
            LIMIT %(lim)s
            """,
            params,
        )
        rows = cur.fetchall()
    return [_order_row(r) for r in rows]


def list_orderbook_events(user: str, limit: int = 100) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT txhash, log_index, entry_index, block_number, timestamp, market,
                   flag, is_buy, action, price, order_id, size
            FROM crystal_orderbook_events
            WHERE user_address = %s
            ORDER BY timestamp DESC, block_number DESC, log_index DESC, entry_index DESC, txhash DESC
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


def batch_insert_market_trades(rows: list[tuple], cur) -> None:
    if not rows:
        return
    execute_values(
        cur,
        """
        INSERT INTO crystal_market_trades
            (txhash, log_index, block_number, timestamp, market, user_address,
             is_buy, amount_in, amount_out, start_price, end_price)
        VALUES %s
        ON CONFLICT (txhash, log_index) DO NOTHING
        """,
        rows,
    )


def list_exchange_trades(
    user, market: str | None = None, limit: int = 100, before_ts: int | None = None
) -> list[dict[str, Any]]:
    addr = _addr_list(user)
    mkt = (market or "").lower() if market else None
    cutoff = int(before_ts) if before_ts is not None else None

    taker_where = "user_address = ANY(%(u)s)"
    maker_where = "maker = ANY(%(u)s)"
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
                SELECT 'taker' AS side_kind, txhash, MIN(log_index) AS log_index,
                       MIN(block_number) AS block_number, MIN(timestamp) AS timestamp,
                       market, is_buy, SUM(amount_in) AS amount_in, SUM(amount_out) AS amount_out,
                       (ARRAY_AGG(start_price ORDER BY log_index))[1] AS start_price,
                       (ARRAY_AGG(end_price ORDER BY log_index DESC))[1] AS end_price,
                       NULL::bigint AS order_id, COUNT(*) AS legs
                FROM crystal_market_trades WHERE {taker_where}
                GROUP BY txhash, market, is_buy
                UNION ALL
                SELECT 'maker' AS side_kind, txhash, MIN(log_index) AS log_index,
                       MIN(block_number) AS block_number, MIN(timestamp) AS timestamp,
                       market, maker_is_buy AS is_buy, SUM(amount_out) AS amount_in,
                       SUM(amount_high) AS amount_out,
                       (ARRAY_AGG(price ORDER BY log_index))[1] AS start_price,
                       (ARRAY_AGG(price ORDER BY log_index DESC))[1] AS end_price,
                       CASE WHEN COUNT(*) = 1 THEN MIN(order_id) END AS order_id, COUNT(*) AS legs
                FROM crystal_orderbook_fills WHERE {maker_where}
                GROUP BY txhash, market, maker_is_buy
            ) u
            ORDER BY timestamp DESC, block_number DESC, log_index DESC, txhash DESC, is_buy DESC
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
            "legs": int(lg),
        }
        for k, tx, li, bn, ts, m, b, ai, ao, sp, ep, oid, lg in rows
    ]


def list_order_history(
    user, market: str | None = None, limit: int = 100, before_ts: int | None = None
) -> list[dict[str, Any]]:
    addr = _addr_list(user)
    mkt = (market or "").lower() if market else None
    cutoff = int(before_ts) if before_ts is not None else None

    ev_where = "user_address = ANY(%(u)s)"
    fill_where = "maker = ANY(%(u)s)"
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
            ORDER BY timestamp DESC, block_number DESC, log_index DESC, entry_index DESC, txhash DESC
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


def open_order_locked_by_token(user) -> dict[str, int]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT LOWER(CASE WHEN o.is_buy THEN m.quote_address ELSE m.base_address END) AS token,
                   SUM(o.size) AS locked
            FROM crystal_orderbook_orders o
            JOIN crystal_markets m ON m.market = o.market
            WHERE o.user_address = ANY(%s) AND o.status = 'open' AND o.size > 0
            GROUP BY 1
            """,
            (_addr_list(user),),
        )
        return {t: int(v) for t, v in cur.fetchall() if t}


def market_klines(market: str, res: int, limit: int = 3000) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT (timestamp / %(res)s) * %(res)s AS bucket_ts,
                   (ARRAY_AGG(start_price ORDER BY timestamp, block_number, log_index))[1] AS open,
                   MAX(GREATEST(start_price, end_price)) AS high,
                   MIN(LEAST(start_price, end_price)) AS low,
                   (ARRAY_AGG(end_price ORDER BY timestamp DESC, block_number DESC, log_index DESC))[1] AS close,
                   SUM(CASE WHEN is_buy THEN amount_out ELSE amount_in END) AS base_volume,
                   SUM(CASE WHEN is_buy THEN amount_in ELSE amount_out END) AS quote_volume
            FROM crystal_market_trades
            WHERE market = %(m)s
            GROUP BY bucket_ts
            ORDER BY bucket_ts DESC
            LIMIT %(lim)s
            """,
            {"res": int(res), "m": (market or "").lower(), "lim": int(limit)},
        )
        rows = cur.fetchall()
    return [
        {
            "time": int(t),
            "open": str(int(o or 0)),
            "high": str(int(h or 0)),
            "low": str(int(lo or 0)),
            "close": str(int(c or 0)),
            "baseVolume": str(int(bv or 0)),
            "quoteVolume": str(int(qv or 0)),
        }
        for t, o, h, lo, c, bv, qv in rows
    ]


def get_wallet_prefs(key: str) -> dict[str, Any] | None:
    with db_cursor() as cur:
        cur.execute("SELECT wallet_count, selected, updated_at FROM crystal_wallet_prefs WHERE key = %s", (key,))
        row = cur.fetchone()
    if not row:
        return None
    count, selected, updated = row
    return {"count": int(count or 0), "selected": list(selected or []), "updatedAt": int(updated or 0)}


def put_wallet_prefs(key: str, count: int, selected: list[int], updated_at: int) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO crystal_wallet_prefs (key, wallet_count, selected, updated_at)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (key) DO UPDATE SET
                wallet_count = EXCLUDED.wallet_count,
                selected = EXCLUDED.selected,
                updated_at = EXCLUDED.updated_at
            WHERE crystal_wallet_prefs.updated_at <= EXCLUDED.updated_at
            """,
            (key, int(count), json.dumps([int(i) for i in selected]), int(updated_at)),
        )
