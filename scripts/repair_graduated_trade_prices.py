"""Repoint post-graduation launchpad trade prices at the pool mid.

Trades on a graduated crystal market were priced from the Trade event's
end_price, which is the fill price: it sits on the ask after a buy and the bid
after a sell, roughly the fee either side of the mid. A buy then a sell therefore
prints a ~2% move on a market that did not move. The Sync emitted in the same
transaction carries the post-trade reserves, so the mid is recoverable per trade.

Dry run by default. Pass --apply to write, which also snapshots every row it is
about to change to a json file first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage as storage  # noqa: E402
from core.storage import db_cursor  # noqa: E402

SELECT_SQL = """
    SELECT t.txhash,
           t.log_index,
           t.block_number,
           t.token,
           t.is_buy,
           t.price_native AS recorded,
           s.reserve_quote / s.reserve_base AS mid
    FROM launchpad_trades t
    JOIN launchpad_tokens k
      ON k.token = t.token AND k.source = 0 AND k.migrated
    JOIN LATERAL (
        SELECT reserve_quote, reserve_base
        FROM crystal_pool_sync_events s2
        WHERE s2.txhash = t.txhash AND s2.reserve_base > 0
        ORDER BY s2.log_index DESC
        LIMIT 1
    ) s ON TRUE
    WHERE t.block_number >= k.migrated_block
      AND t.native_reserve = 0
      AND t.token_reserve = 0
      AND t.price_native IS DISTINCT FROM s.reserve_quote / s.reserve_base
    ORDER BY t.block_number
"""

UPDATE_SQL = """
    UPDATE launchpad_trades
    SET price_native = %s
    WHERE txhash = %s AND log_index = %s
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the corrections")
    ap.add_argument("--snapshot", default="graduated_price_repair_snapshot.json")
    args = ap.parse_args()

    storage.init_pool()

    with db_cursor() as cur:
        cur.execute(SELECT_SQL)
        rows = cur.fetchall()

    if not rows:
        print("nothing to repair")
        return 0

    worst = Decimal(0)
    snapshot = []
    for txhash, log_index, block_number, token, is_buy, recorded, mid in rows:
        rec = Decimal(recorded or 0)
        err = ((rec / Decimal(mid)) - 1) * 100 if mid else Decimal(0)
        worst = max(worst, abs(err))
        snapshot.append(
            {
                "txhash": txhash,
                "log_index": int(log_index),
                "block_number": int(block_number),
                "token": token,
                "is_buy": bool(is_buy),
                "price_native_before": str(rec),
                "price_native_after": str(mid),
                "error_pct": f"{err:+.3f}",
            }
        )
        print(f"  blk {block_number} {'BUY ' if is_buy else 'SELL'} {rec} -> {mid}  ({err:+.2f}%)")

    print(f"\n{len(rows)} trades to correct, worst error {worst:.2f}%")

    if not args.apply:
        print("dry run: pass --apply to write")
        return 0

    with open(args.snapshot, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)
    print(f"snapshot written to {args.snapshot}")

    with db_cursor() as cur:
        for row in snapshot:
            cur.execute(UPDATE_SQL, (Decimal(row["price_native_after"]), row["txhash"], row["log_index"]))
    print(f"updated {len(snapshot)} rows")
    print("now rebuild launchpad_ohlcv for the affected tokens and reset last_price_native/ath")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
