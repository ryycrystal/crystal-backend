import time

from psycopg2.extras import execute_values

import core.storage as storage


# rebuild the current-order plane from the immutable events and fills tables:
# read both, fold in chain order in memory, and land the result in one guarded
# bulk upsert so rows the live indexer has already moved past stay untouched
def main() -> None:
    storage.init_pool()
    started = time.monotonic()

    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT market, price, order_id, block_number, log_index, entry_index,
                   action, is_buy, user_address, size, timestamp
            FROM crystal_orderbook_events
            """
        )
        events = cur.fetchall()
        cur.execute(
            """
            SELECT market, price, order_id, block_number, log_index,
                   maker_is_buy, remaining, timestamp
            FROM crystal_orderbook_fills
            """
        )
        fills = cur.fetchall()
    print(f"[REBUILD] {len(events)} events, {len(fills)} fills loaded", flush=True)

    ops = []
    for m, p, oid, bn, li, ei, action, b, u, s, ts in events:
        ops.append((int(bn), int(li), int(ei), action, m, int(p), int(oid), bool(b), u, int(s), int(ts)))
    for m, p, oid, bn, li, b, rem, ts in fills:
        ops.append((int(bn), int(li), 0, "fill", m, int(p), int(oid), bool(b), None, int(rem), int(ts)))
    ops.sort(key=lambda x: (x[0], x[1], x[2]))

    orders: dict[tuple, dict] = {}
    for bn, li, ei, action, m, p, oid, is_buy, user, size, ts in ops:
        key = (m, p, oid)
        row = orders.get(key)
        if row is None:
            row = orders[key] = {
                "user": user or "",
                "is_buy": is_buy,
                "size": 0,
                "original": 0,
                "status": "canceled",
                "cb": bn,
                "cts": ts,
                "ub": bn,
                "uts": ts,
            }
        row["ub"], row["uts"] = bn, ts
        if action == "add":
            row.update(user=user or row["user"], is_buy=is_buy, size=size, original=size, status="open")
        elif action == "remove":
            row.update(size=0, status="filled" if row["status"] == "filled" else "canceled")
        elif action == "decrease":
            row["size"] = max(row["size"] - size, 0)
        elif action == "fill":
            row["size"] = size
            if size == 0:
                row["status"] = "filled"

    rows = [
        (m, p, oid, r["user"], r["is_buy"], r["size"], r["original"], r["status"], r["cb"], r["cts"], r["ub"], r["uts"])
        for (m, p, oid), r in orders.items()
    ]
    print(f"[REBUILD] folded into {len(rows)} orders", flush=True)

    with storage.db_cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO crystal_orderbook_orders
                (market, price, order_id, user_address, is_buy, size, original_size, status,
                 created_block, created_ts, updated_block, updated_ts)
            VALUES %s
            ON CONFLICT (market, price, order_id) DO UPDATE SET
                user_address = EXCLUDED.user_address,
                is_buy = EXCLUDED.is_buy,
                size = EXCLUDED.size,
                original_size = EXCLUDED.original_size,
                status = EXCLUDED.status,
                updated_block = EXCLUDED.updated_block,
                updated_ts = EXCLUDED.updated_ts
            WHERE crystal_orderbook_orders.updated_block <= EXCLUDED.updated_block
            """,
            rows,
            page_size=5000,
        )
    print(f"[REBUILD] complete in {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
