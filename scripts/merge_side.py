"""Copy replayed, corrected rows from the side database into prod for chosen tokens.

Dry run by default. --apply writes, per token, in one short transaction, after
snapshotting every prod row it will change.

The side replay covers history up to a cutoff block (the last hot block it was
given). Prod keeps trading meanwhile, so the merge is cutoff-aware:

  launchpad_trades       prod rows at or before the cutoff are replaced with the side
                         rows; prod rows after the cutoff are kept. A trade prod already
                         had keeps prod's usd_amount; a trade prod was missing is priced
                         at the MON/USD rate prod used for the nearest trade of the same
                         token, because the side replay never sees the oracle pool's
                         blocks and its own usd figures are at today's rate.
  launchpad_positions    trade-derived columns for rows prod has; rows prod lacks are
                         inserted whole. balance_token on existing rows is left as prod
                         has it (reconciled to chain separately). A wallet with a prod
                         trade after the cutoff is deferred: none of its rows change.
  launchpad_tokens       volumes and counts recomputed from the final trade set,
                         fees_usd scaled by the volume_usd ratio.
  launchpad_ohlcv        buckets that close before the cutoff are replaced with the side
                         candles (keeping prod's mon_usd per bucket); later buckets stay.
  launchpad_users        recomputed from prod trades for every user of the token.

A token whose pre-cutoff prod trades are not all present in the side set is
reported and skipped, because replacing would drop rows.

    DATABASE_URL=<side> PROD_PGHOSTADDR=127.0.0.1 PROD_PGPORT=15433 \\
      python scripts/merge_side.py --tokens-file worker0_tokens.json --blocks-file worker0_blocks.json
      python scripts/merge_side.py --tokens-file worker0_tokens.json --blocks-file worker0_blocks.json --apply
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from decimal import Decimal

import psycopg2
import psycopg2.errors
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_loader import load_env  # noqa: E402

load_env()

EXPECTED_PGHOST_MARKER = "crystal-prod-db-r3"
MERGE_LOCK_KEY = 782301944118
WRITE_ATTEMPTS = 8
WAD = Decimal(10) ** 18

POSITION_COLS = (
    "token_bought",
    "token_sold",
    "native_spent",
    "native_received",
    "cost_basis_native",
    "realized_pnl_native",
    "trade_count",
    "buy_count",
    "sell_count",
)


def side_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def prod_conn():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        hostaddr=os.environ.get("PROD_PGHOSTADDR") or None,
        port=int(os.environ.get("PROD_PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"],
        sslmode="require",
        connect_timeout=30,
    )


def columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def shared_columns(sc, pc, table):
    prod = set(columns(pc, table))
    return [c for c in columns(sc, table) if c in prod]


def snapshot(fh, kind, token, rows):
    for r in rows:
        fh.write(json.dumps({"kind": kind, "token": token, "row": [str(v) for v in r]}) + "\n")


def nearest(points: list[tuple[int, Decimal]], keys: list[int], ts: int) -> Decimal | None:
    if not points:
        return None
    i = bisect.bisect_left(keys, ts)
    candidates = [points[j] for j in (i - 1, i) if 0 <= j < len(points)]
    return min(candidates, key=lambda p: abs(p[0] - ts))[1]


def reprice_trades(ix, side_trades, prod_trades, hot=None):
    key = lambda r: (r[ix["txhash"]].lower(), int(r[ix["log_index"]]))  # noqa: E731
    prod_by_key = {key(r): r for r in prod_trades}
    points = sorted(
        (int(r[ix["timestamp"]]), Decimal(r[ix["usd_amount"]]) / (Decimal(r[ix["native_amount"]]) / WAD))
        for r in prod_trades
        if r[ix["native_amount"]] and Decimal(r[ix["native_amount"]]) > 0 and r[ix["usd_amount"]] is not None
    )
    keys = [p[0] for p in points]

    out, new_rows = [], 0
    for r in side_trades:
        row = list(r)
        k = key(row)
        if k in prod_by_key:
            row[ix["usd_amount"]] = prod_by_key[k][ix["usd_amount"]]
        else:
            new_rows += 1
            rate = nearest(points, keys, int(row[ix["timestamp"]]))
            if rate is not None and row[ix["native_amount"]] is not None:
                row[ix["usd_amount"]] = (Decimal(row[ix["native_amount"]]) / WAD) * rate
        out.append(tuple(row))
    side_keys = {key(r) for r in side_trades}
    synthetic = {key(r) for r in prod_trades if "venue" in ix and r[ix["venue"]] == "reconciliation"}
    unmatched = [k for k in prod_by_key if k not in side_keys and k not in synthetic]
    if hot is None:
        dropped = unmatched
    else:
        dropped = [k for k in unmatched if int(prod_by_key[k][ix["block_number"]]) not in hot]
    return out, new_rows, dropped, len(unmatched) - len(dropped)


def carry_mon_usd(ohlcv_cols, side_ohlcv, prod_ohlcv):
    ix = {c: i for i, c in enumerate(ohlcv_cols)}
    exact = {(int(r[ix["resolution_sec"]]), int(r[ix["bucket_start"]])): r[ix["mon_usd"]] for r in prod_ohlcv}
    by_res: dict[int, list[tuple[int, Decimal]]] = {}
    for r in prod_ohlcv:
        if r[ix["mon_usd"]] is not None:
            by_res.setdefault(int(r[ix["resolution_sec"]]), []).append((int(r[ix["bucket_start"]]), r[ix["mon_usd"]]))
    for v in by_res.values():
        v.sort()
    keys = {res: [p[0] for p in pts] for res, pts in by_res.items()}

    out = []
    for r in side_ohlcv:
        row = list(r)
        res, bucket = int(row[ix["resolution_sec"]]), int(row[ix["bucket_start"]])
        if (res, bucket) in exact and exact[(res, bucket)] is not None:
            row[ix["mon_usd"]] = exact[(res, bucket)]
        elif res in by_res:
            row[ix["mon_usd"]] = nearest(by_res[res], keys[res], bucket)
        out.append(tuple(row))
    return out


def aggregates(ix, rows):
    agg = {
        "native_volume": Decimal(0),
        "token_volume": Decimal(0),
        "buy_count": 0,
        "sell_count": 0,
        "tx_count": len(rows),
        "volume_usd": Decimal(0),
    }
    for r in rows:
        agg["native_volume"] += Decimal(r[ix["native_amount"]] or 0)
        agg["token_volume"] += Decimal(r[ix["token_amount"]] or 0)
        agg["volume_usd"] += Decimal(r[ix["usd_amount"]] or 0)
        if r[ix["is_buy"]]:
            agg["buy_count"] += 1
        else:
            agg["sell_count"] += 1
    return agg


def merge_token(sc, pc, token, cutoff, cutoff_ts, apply, fh, hot=None):
    trade_cols = [c for c in shared_columns(sc, pc, "launchpad_trades") if c != "id"]
    ohlcv_cols = shared_columns(sc, pc, "launchpad_ohlcv")
    pos_cols = ("user_address", "balance_token", *POSITION_COLS)
    ix = {c: i for i, c in enumerate(trade_cols)}
    oix = {c: i for i, c in enumerate(ohlcv_cols)}

    with sc.cursor() as s:
        s.execute(
            f"SELECT {','.join(trade_cols)} FROM launchpad_trades WHERE token=%s AND block_number<=%s",
            (token, cutoff),
        )
        side_trades = s.fetchall()
        s.execute(f"SELECT {','.join(pos_cols)} FROM launchpad_positions WHERE token=%s", (token,))
        side_positions = s.fetchall()
        s.execute(f"SELECT {','.join(ohlcv_cols)} FROM launchpad_ohlcv WHERE token=%s", (token,))
        side_ohlcv = s.fetchall()

    with pc.cursor() as p:
        p.execute(f"SELECT {','.join(trade_cols)} FROM launchpad_trades WHERE token=%s", (token,))
        prod_trades = p.fetchall()
        p.execute(f"SELECT {','.join(pos_cols)} FROM launchpad_positions WHERE token=%s", (token,))
        prod_positions = p.fetchall()
        p.execute("SELECT volume_usd, fees_usd FROM launchpad_tokens WHERE token=%s", (token,))
        prod_agg = p.fetchone()
        p.execute(f"SELECT {','.join(ohlcv_cols)} FROM launchpad_ohlcv WHERE token=%s", (token,))
        prod_ohlcv = p.fetchall()
    pc.rollback()

    prod_le = [r for r in prod_trades if int(r[ix["block_number"]]) <= cutoff]
    prod_gt = [r for r in prod_trades if int(r[ix["block_number"]]) > cutoff]
    late_users = {r[ix["user_address"]] for r in prod_gt}

    repriced, new_trades, dropped, replaced = reprice_trades(ix, side_trades, prod_le, hot)
    side_keep = [r for r in repriced if r[ix["user_address"]] not in late_users]
    prod_le_keep = [r for r in prod_le if r[ix["user_address"]] in late_users]
    final_rows = prod_gt + prod_le_keep + side_keep

    prod_users = {r[0] for r in prod_positions}
    prod_values = {r[0]: tuple(Decimal(v or 0) for v in r[2:]) for r in prod_positions}
    pos_update = [
        r
        for r in side_positions
        if r[0] in prod_users and r[0] not in late_users and tuple(Decimal(v or 0) for v in r[2:]) != prod_values[r[0]]
    ]
    pos_insert = [r for r in side_positions if r[0] not in prod_users and r[0] not in late_users]
    deferred = [r[0] for r in side_positions if r[0] in late_users]

    agg = aggregates(ix, final_rows)
    prod_volume_usd = Decimal(prod_agg[0] or 0) if prod_agg else Decimal(0)
    prod_fees_usd = Decimal(prod_agg[1] or 0) if prod_agg else Decimal(0)
    fees_usd = prod_fees_usd * agg["volume_usd"] / prod_volume_usd if prod_volume_usd > 0 else prod_fees_usd

    closed = lambda r: int(r[oix["bucket_start"]]) + int(r[oix["resolution_sec"]]) <= cutoff_ts  # noqa: E731
    candles = [r for r in carry_mon_usd(ohlcv_cols, side_ohlcv, prod_ohlcv) if closed(r)]
    prod_candles_replaced = [r for r in prod_ohlcv if closed(r)]

    print(
        f"{token}  trades prod {len(prod_trades):,} ({len(prod_gt)} after cutoff) -> {len(final_rows):,} "
        f"(+{new_trades:,} new, {replaced} replaced, {len(dropped)} dropped)   positions update {len(pos_update):,} "
        f"insert {len(pos_insert):,} deferred {len(deferred)}   candles {len(prod_candles_replaced):,} -> {len(candles):,}   "
        f"volume_usd {prod_volume_usd:,.0f} -> {agg['volume_usd']:,.0f}",
        flush=True,
    )
    if not side_trades:
        print("   side has no trades for this token, skipping", flush=True)
        return
    if dropped:
        print(
            f"   prod has {len(dropped)} pre-cutoff trades in blocks the replay never saw, e.g. {dropped[:3]}; skipping",
            flush=True,
        )
        return
    if not apply:
        return

    with pc.cursor() as p:
        p.execute("DROP TABLE IF EXISTS tmp_side_trades")
        p.execute("DROP TABLE IF EXISTS tmp_side_ohlcv")
        p.execute("CREATE TEMP TABLE tmp_side_trades (LIKE launchpad_trades INCLUDING DEFAULTS)")
        p.execute("CREATE TEMP TABLE tmp_side_ohlcv (LIKE launchpad_ohlcv INCLUDING DEFAULTS)")
        psycopg2.extras.execute_values(
            p, f"INSERT INTO tmp_side_trades ({','.join(trade_cols)}) VALUES %s", side_keep, page_size=1000
        )
        if candles:
            psycopg2.extras.execute_values(
                p, f"INSERT INTO tmp_side_ohlcv ({','.join(ohlcv_cols)}) VALUES %s", candles, page_size=1000
            )
    pc.commit()

    for attempt in range(WRITE_ATTEMPTS):
        try:
            write_token(
                pc,
                token,
                cutoff,
                cutoff_ts,
                fh,
                trade_cols,
                ohlcv_cols,
                pos_cols,
                prod_le,
                prod_positions,
                prod_candles_replaced,
                late_users,
                side_keep,
                pos_update,
                pos_insert,
                agg,
                fees_usd,
                candles,
            )
            break
        except (psycopg2.errors.LockNotAvailable, psycopg2.errors.DeadlockDetected, psycopg2.OperationalError) as e:
            pc.rollback()
            wait = 5 + 7 * attempt
            print(f"   write attempt {attempt + 1} failed ({type(e).__name__}); retrying in {wait}s", flush=True)
            time.sleep(wait)
    else:
        print("   GAVE UP on this token after repeated lock timeouts", flush=True)
        return
    with pc.cursor() as p:
        p.execute("DROP TABLE IF EXISTS tmp_side_trades")
        p.execute("DROP TABLE IF EXISTS tmp_side_ohlcv")
    pc.commit()
    print("   committed", flush=True)


def write_token(
    pc,
    token,
    cutoff,
    cutoff_ts,
    fh,
    trade_cols,
    ohlcv_cols,
    pos_cols,
    prod_le,
    prod_positions,
    prod_candles_replaced,
    late_users,
    side_keep,
    pos_update,
    pos_insert,
    agg,
    fees_usd,
    candles,
):
    with pc:
        with pc.cursor() as p:
            p.execute("SET LOCAL lock_timeout = 0")
            p.execute("SELECT pg_advisory_xact_lock(%s)", (MERGE_LOCK_KEY,))
            p.execute("SET LOCAL lock_timeout = '5s'")
            fh.write(
                json.dumps(
                    {
                        "kind": "meta",
                        "token": token,
                        "cutoff": cutoff,
                        "cutoff_ts": cutoff_ts,
                        "trade_cols": trade_cols,
                        "ohlcv_cols": ohlcv_cols,
                        "pos_cols": list(pos_cols),
                        "late_users": sorted(late_users),
                        "inserted_users": [r[0] for r in pos_insert],
                    }
                )
                + "\n"
            )
            snapshot(fh, "trade", token, prod_le)
            snapshot(fh, "position", token, prod_positions)
            p.execute("SELECT * FROM launchpad_tokens WHERE token=%s", (token,))
            snapshot(fh, "token", token, [p.fetchone() or ()])
            snapshot(fh, "ohlcv", token, prod_candles_replaced)
            fh.flush()

            p.execute(
                "DELETE FROM launchpad_trades WHERE token=%s AND block_number<=%s AND NOT (user_address = ANY(%s))",
                (token, cutoff, list(late_users)),
            )
            p.execute(
                f"INSERT INTO launchpad_trades ({','.join(trade_cols)}) "
                f"SELECT {','.join(trade_cols)} FROM tmp_side_trades ON CONFLICT DO NOTHING"
            )

            if pos_update:
                sets = ", ".join(f"{c}=data.{c}" for c in POSITION_COLS)
                psycopg2.extras.execute_values(
                    p,
                    f"""
                    UPDATE launchpad_positions AS lp SET {sets}
                    FROM (VALUES %s) AS data(user_address,token,{",".join(POSITION_COLS)})
                    WHERE lp.user_address = data.user_address AND lp.token = data.token
                    """,
                    [(row[0], token, *row[2:]) for row in pos_update],
                    page_size=1000,
                )
            if pos_insert:
                psycopg2.extras.execute_values(
                    p,
                    f"""
                    INSERT INTO launchpad_positions (user_address,token,{",".join(pos_cols[1:])})
                    VALUES %s ON CONFLICT DO NOTHING
                    """,
                    [(row[0], token, *row[1:]) for row in pos_insert],
                    page_size=1000,
                )

            p.execute(
                """
                UPDATE launchpad_tokens SET native_volume=%s, token_volume=%s, buy_count=%s, sell_count=%s,
                    tx_count=%s, volume_usd=%s, fees_usd=%s WHERE token=%s
                """,
                (
                    agg["native_volume"],
                    agg["token_volume"],
                    agg["buy_count"],
                    agg["sell_count"],
                    agg["tx_count"],
                    agg["volume_usd"],
                    fees_usd,
                    token,
                ),
            )

            p.execute(
                "DELETE FROM launchpad_ohlcv WHERE token=%s AND bucket_start + resolution_sec <= %s",
                (token, cutoff_ts),
            )
            if candles:
                p.execute(
                    f"INSERT INTO launchpad_ohlcv ({','.join(ohlcv_cols)}) "
                    f"SELECT {','.join(ohlcv_cols)} FROM tmp_side_ohlcv ON CONFLICT DO NOTHING"
                )

            p.execute(
                """
                INSERT INTO launchpad_users (address, total_native_volume, total_trades, total_realized_pnl_native)
                SELECT user_address, COALESCE(SUM(native_amount),0), COUNT(*), COALESCE(SUM(realized_native),0)
                FROM launchpad_trades
                WHERE user_address IN (SELECT user_address FROM launchpad_positions WHERE token=%s)
                GROUP BY user_address
                ON CONFLICT (address) DO UPDATE SET
                    total_native_volume = EXCLUDED.total_native_volume,
                    total_trades = EXCLUDED.total_trades,
                    total_realized_pnl_native = EXCLUDED.total_realized_pnl_native
                """,
                (token,),
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", action="append", default=[])
    ap.add_argument("--tokens-file")
    ap.add_argument("--blocks-file", help="hot-block list the side replay ran on; its last block is the cutoff")
    ap.add_argument("--cutoff", type=int, help="explicit cutoff block instead of --blocks-file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--snapshot", default="merge_side_snapshot.jsonl")
    args = ap.parse_args()

    tokens = [t.lower() for t in args.token]
    if args.tokens_file:
        tokens += [t.lower() for t in json.load(open(args.tokens_file))]
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        raise SystemExit("pass --token or --tokens-file")
    if "crystal_replay" not in os.environ.get("DATABASE_URL", ""):
        raise SystemExit("DATABASE_URL must point at the side database")
    if args.apply and EXPECTED_PGHOST_MARKER not in os.environ.get("PGHOST", ""):
        raise SystemExit(f"refusing to --apply: PGHOST is not {EXPECTED_PGHOST_MARKER}")
    cutoff = args.cutoff
    hot = None
    if args.blocks_file:
        hot = {int(b) for b in json.load(open(args.blocks_file))}
        if cutoff is None:
            cutoff = max(hot)
    if cutoff is None:
        raise SystemExit("pass --cutoff or --blocks-file")

    sc = side_conn()
    pc = prod_conn()
    with sc.cursor() as cur:
        cur.execute("SELECT MAX(timestamp) FROM launchpad_trades WHERE block_number<=%s", (cutoff,))
        cutoff_ts = int(cur.fetchone()[0] or 0)
    print(
        f"{'APPLY' if args.apply else 'DRY RUN'}: {len(tokens)} tokens   cutoff block {cutoff:,} (ts {cutoff_ts})   "
        f"snapshot -> {args.snapshot}",
        flush=True,
    )
    t0 = time.time()
    with open(args.snapshot, "a", encoding="utf-8") as fh:
        for i, token in enumerate(tokens, 1):
            print(f"[{i}/{len(tokens)}] ", end="")
            merge_token(sc, pc, token, cutoff, cutoff_ts, args.apply, fh, hot)
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
