import argparse
import csv
import gzip
import os
from decimal import Decimal

from psycopg2.extras import execute_values

import core.storage as storage

# must match state.CURVE_LIVE_SECONDS: the rule being applied retroactively is the live one
CURVE_LIVE_SECONDS = 7 * 86400
RESOLUTIONS = (1, 5, 15, 60, 300, 900, 3600, 14400, 86400)


def _affected_tokens(cur) -> list[str]:
    cur.execute(
        """SELECT DISTINCT t.token FROM launchpad_trades t
           JOIN launchpad_tokens k ON k.token = t.token
           WHERE t.venue = 'pool' AND k.migrated = false"""
    )
    return sorted(r[0] for r in cur.fetchall())


def _replay(trades, hold: bool):
    """Price each trade in order, holding side pool swaps on a live curve when `hold` is set."""
    mid = None
    last_curve = 0
    priced = []
    for ts, _log_index, venue, price in trades:
        price = Decimal(price or 0)
        if venue == "curve":
            mid = price
            last_curve = ts
        elif hold and venue == "pool" and last_curve and ts - last_curve <= CURVE_LIVE_SECONDS and mid:
            price = mid
        else:
            mid = price
        priced.append((ts, price))
    return priced


def _candles(priced, res: int) -> dict:
    """High, low and close per bucket, with each bucket's open (the price before its first trade)
    inside the range. The indexer seeds a candle from that open, which is how a pool price in one
    bucket reached the low of the next: its open was the pool price."""
    out = {}
    prev = None
    for ts, price in priced:
        start = (ts // res) * res
        if start not in out:
            seed = [prev] if prev and prev > 0 else []
            out[start] = seed
        if price > 0:
            out[start].append(price)
        prev = price if price > 0 else prev
    return {b: (max(v), min(v), v[-1]) for b, v in out.items() if v}


def main() -> None:
    ap = argparse.ArgumentParser(description="undo side pool spikes in stored candles for un-migrated tokens")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--token", help="limit to one token")
    ap.add_argument("--snapshot-dir", required=True, help="where the before values of changed rows are written")
    args = ap.parse_args()

    storage.init_pool()
    with storage.db_cursor() as cur:
        tokens = [args.token.lower()] if args.token else _affected_tokens(cur)
    print(f"[CANDLES] {len(tokens)} un-migrated tokens with pool trades")

    os.makedirs(args.snapshot_dir, exist_ok=True)
    snap = gzip.open(
        os.path.join(args.snapshot_dir, "launchpad_ohlcv_before.csv.gz"), "wt", newline="", encoding="utf-8"
    )
    writer = csv.writer(snap)
    writer.writerow(["token", "resolution_sec", "bucket_start", "high_price", "low_price", "close_price"])

    touched_tokens = changed_rows = held_trades = 0
    for token in tokens:
        # read, close the transaction, compute, then open a short one to write
        with storage.db_cursor() as cur:
            cur.execute(
                """SELECT timestamp, log_index, venue, price_native FROM launchpad_trades
                   WHERE token = %s ORDER BY timestamp, block_number, log_index""",
                (token,),
            )
            trades = cur.fetchall()
        raw = _replay(trades, hold=False)
        fixed = _replay(trades, hold=True)
        held = sum(1 for (_, a), (_, b) in zip(raw, fixed, strict=True) if a != b)
        if not held:
            continue
        held_trades += held

        # the only values ever replaced are ones that literally are a held pool price. rebuilding
        # whole candles instead would re-derive untouched fields under this script's idea of an
        # open, which does not match how pre-2026-09-14 candles were stored and moved an hourly
        # low that was never spiked
        spikes = [r for (_, r), (_, f) in zip(raw, fixed, strict=True) if r != f and r > 0]

        def _is_spike(value, spikes=spikes):
            v = Decimal(value or 0)
            return v > 0 and any(abs(v - sp) <= sp * Decimal("1e-6") for sp in spikes)

        wanted = {}
        for res in RESOLUTIONS:
            before = _candles(raw, res)
            for start, candle in _candles(fixed, res).items():
                if before.get(start) != candle:
                    wanted[(res, start)] = candle

        if not wanted:
            continue
        with storage.db_cursor() as cur:
            cur.execute(
                """SELECT resolution_sec, bucket_start, high_price, low_price, close_price
                   FROM launchpad_ohlcv WHERE token = %s""",
                (token,),
            )
            stored = {(r, b): (h, lo, c) for r, b, h, lo, c in cur.fetchall()}

        updates = []
        for key, (hi, lo, cl) in wanted.items():
            cur_row = stored.get(key)
            if cur_row is None:
                continue
            new = (
                hi if _is_spike(cur_row[0]) else Decimal(cur_row[0]),
                lo if _is_spike(cur_row[1]) else Decimal(cur_row[1]),
                cl if _is_spike(cur_row[2]) else Decimal(cur_row[2]),
            )
            if new != tuple(Decimal(x) for x in cur_row):
                writer.writerow([token, key[0], key[1], *cur_row])
                updates.append((*new, token, key[0], key[1]))

        if updates:
            touched_tokens += 1
            changed_rows += len(updates)
            if args.apply:
                # one statement per page rather than a round trip per row: a row at a time kept a
                # single token's transaction open for minutes over the link, holding row locks the
                # indexer's candle upserts can queue behind
                with storage.db_cursor() as cur:
                    execute_values(
                        cur,
                        """UPDATE launchpad_ohlcv AS o
                           SET high_price = v.hi, low_price = v.lo, close_price = v.cl
                           FROM (VALUES %s) AS v(hi, lo, cl, token, res, bucket)
                           WHERE o.token = v.token AND o.resolution_sec = v.res AND o.bucket_start = v.bucket""",
                        updates,
                        template="(%s::numeric, %s::numeric, %s::numeric, %s, %s::int, %s::bigint)",
                        page_size=5000,
                    )
                    cur.connection.commit()

    snap.close()
    verb = "updated" if args.apply else "would update"
    print(f"[CANDLES] held {held_trades:,} pool trades; {verb} {changed_rows:,} candles across {touched_tokens} tokens")
    if not args.apply:
        print("[CANDLES] dry run. pass --apply to write")


if __name__ == "__main__":
    main()
