"""Backfill launchpad_ohlcv.mon_usd for candles written before the column existed.

The indexer stamps each new candle with the mon/usd rate in force at that point
in the block stream, but every candle older than that column reads 0, so a
mon <-> usd toggle has nothing to convert them with. There is no historical rate
series stored anywhere, so this reconstructs one from the trades themselves:
usd_amount / native_amount recovers the rate that was in effect when a trade was
indexed, and the median of a day's trades is a stable estimate of that day.

Daily granularity is deliberate. It is an approximation, not the per-candle rate
the indexer writes going forward.

Dry run by default. Pass --apply to write. Batched on the primary key so no
single transaction holds a lock on a 9GB table, and resumable: rerunning only
touches rows still at 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage as storage  # noqa: E402
from core.storage import db_cursor  # noqa: E402

DAY = 86400
MIN_TRADE_WEI = 10**16

DAILY_RATES_SQL = """
    SELECT (timestamp / %s) * %s AS day,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY usd_amount / (native_amount / 1e18)) AS rate
    FROM launchpad_trades
    WHERE usd_amount > 0 AND native_amount > %s
    GROUP BY 1
    ORDER BY 1
"""

TOKENS_SQL = "SELECT token FROM launchpad_tokens ORDER BY token"

UPDATE_SQL = """
    UPDATE launchpad_ohlcv o
    SET mon_usd = r.rate
    FROM (SELECT unnest(%s::bigint[]) AS day, unnest(%s::numeric[]) AS rate) r
    WHERE o.token = ANY(%s)
      AND o.mon_usd = 0
      AND (o.bucket_start / 86400) * 86400 = r.day
"""


def load_daily_rates() -> dict[int, Decimal]:
    with db_cursor() as cur:
        cur.execute(DAILY_RATES_SQL, (DAY, DAY, MIN_TRADE_WEI))
        rows = cur.fetchall()
    return {int(day): Decimal(str(rate)) for day, rate in rows if rate and Decimal(str(rate)) > 0}


def build_lookup(rates: dict[int, Decimal]) -> tuple[list[int], dict[int, Decimal]]:
    """Days with no trades inherit the most recent day that had them."""
    if not rates:
        return [], {}
    days = sorted(rates)
    filled: dict[int, Decimal] = {}
    current = rates[days[0]]
    for day in range(days[0], days[-1] + DAY, DAY):
        if day in rates:
            current = rates[day]
        filled[day] = current
    return sorted(filled), filled


def rate_for(bucket_start: int, days: list[int], filled: dict[int, Decimal]) -> Decimal | None:
    if not days:
        return None
    day = (int(bucket_start) // DAY) * DAY
    if day in filled:
        return filled[day]
    # candles outside the traded range clamp to the nearest known day
    return filled[days[0]] if day < days[0] else filled[days[-1]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()

    storage.init_pool()

    rates = load_daily_rates()
    days, filled = build_lookup(rates)
    if not days:
        print("no daily rates could be derived from trades")
        return 1
    print(f"derived {len(rates)} traded days, carried forward across {len(days)} days")
    print(f"  first {days[0]} rate {filled[days[0]]:.8f}")
    print(f"  last  {days[-1]} rate {filled[days[-1]]:.8f}")

    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM launchpad_ohlcv WHERE mon_usd = 0")
        remaining = int(cur.fetchone()[0])
    print(f"candles still at 0: {remaining:,}")

    if not args.apply:
        print("dry run: pass --apply to write")
        return 0

    with db_cursor() as cur:
        cur.execute(TOKENS_SQL)
        tokens = [r[0] for r in cur.fetchall()]
    print(f"sweeping {len(tokens):,} tokens in chunks of {args.batch}")

    day_list = [int(day) for day in days]
    rate_list = [filled[day] for day in days]
    done = 0
    for i in range(0, len(tokens), args.batch):
        chunk = tokens[i : i + args.batch]
        with db_cursor() as cur:
            cur.execute(UPDATE_SQL, (day_list, rate_list, chunk))
            if cur.rowcount and cur.rowcount > 0:
                done += cur.rowcount
        if (i // args.batch) % 25 == 0:
            print(f"  token {i:,}/{len(tokens):,}  filled {done:,}")

    print(f"updated {done:,} candles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
