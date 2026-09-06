"""Roll a merge_side.py --apply back from its snapshot file, per token.

The snapshot holds, per token, a meta line (cutoff, columns, deferred and inserted
users) followed by the prod rows the merge replaced: pre-cutoff trades, every
position row, the token row, and the candles that closed before the cutoff.
Restoring puts exactly those rows back and removes the position rows the merge
inserted. Dry run by default.

    PROD_PGHOSTADDR=127.0.0.1 PROD_PGPORT=15433 \\
      python scripts/restore_side_snapshot.py --snapshot merge_apply_v7.jsonl [--token 0x...] [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_loader import load_env  # noqa: E402

load_env()

EXPECTED_PGHOST_MARKER = "crystal-prod-db-r3"


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


def value(v):
    return None if v == "None" else v


def load(path, only):
    tokens: dict[str, dict] = {}
    for line in open(path, encoding="utf-8"):
        rec = json.loads(line)
        token = rec["token"]
        if only and token not in only:
            continue
        entry = tokens.setdefault(token, {"meta": None, "trade": [], "position": [], "token": [], "ohlcv": []})
        if rec["kind"] == "meta":
            entry["meta"] = rec
            entry.update({"trade": [], "position": [], "token": [], "ohlcv": []})
        else:
            entry[rec["kind"]].append([value(v) for v in rec["row"]])
    return tokens


def restore_token(pc, token, entry, apply):
    meta = entry["meta"]
    if not meta:
        print(f"{token}  no meta line, cannot restore safely; skipping", flush=True)
        return
    trade_cols, ohlcv_cols, pos_cols = meta["trade_cols"], meta["ohlcv_cols"], meta["pos_cols"]
    print(
        f"{token}  restore {len(entry['trade']):,} trades <= {meta['cutoff']:,}, {len(entry['position']):,} positions, "
        f"{len(entry['ohlcv']):,} candles, drop {len(meta['inserted_users'])} inserted positions",
        flush=True,
    )
    if not apply:
        return
    with pc:
        with pc.cursor() as p:
            p.execute("SET lock_timeout = '5s'")
            p.execute("DELETE FROM launchpad_trades WHERE token=%s AND block_number<=%s", (token, meta["cutoff"]))
            if entry["trade"]:
                psycopg2.extras.execute_values(
                    p,
                    f"INSERT INTO launchpad_trades ({','.join(trade_cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    entry["trade"],
                    page_size=1000,
                )
            if meta["inserted_users"]:
                p.execute(
                    "DELETE FROM launchpad_positions WHERE token=%s AND user_address = ANY(%s)",
                    (token, meta["inserted_users"]),
                )
            if entry["position"]:
                cols = pos_cols[1:]
                sets = ", ".join(f"{c}=data.{c}" for c in cols)
                psycopg2.extras.execute_values(
                    p,
                    f"""
                    UPDATE launchpad_positions AS lp SET {sets}
                    FROM (VALUES %s) AS data(user_address,token,{",".join(cols)})
                    WHERE lp.user_address = data.user_address AND lp.token = data.token
                    """,
                    [(row[0], token, *row[1:]) for row in entry["position"]],
                    page_size=1000,
                )
            if entry["token"] and entry["token"][0]:
                p.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='launchpad_tokens' ORDER BY ordinal_position"
                )
                tcols = [r[0] for r in p.fetchall()]
                row = entry["token"][0]
                if len(row) == len(tcols):
                    sets = ", ".join(f"{c}=%s" for c in tcols if c != "token")
                    p.execute(
                        f"UPDATE launchpad_tokens SET {sets} WHERE token=%s",
                        (*[v for c, v in zip(tcols, row) if c != "token"], token),
                    )
            p.execute(
                "DELETE FROM launchpad_ohlcv WHERE token=%s AND bucket_start + resolution_sec <= %s",
                (token, meta["cutoff_ts"]),
            )
            if entry["ohlcv"]:
                psycopg2.extras.execute_values(
                    p,
                    f"INSERT INTO launchpad_ohlcv ({','.join(ohlcv_cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    entry["ohlcv"],
                    page_size=1000,
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
    print("   restored", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--token", action="append", default=[])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply and EXPECTED_PGHOST_MARKER not in os.environ.get("PGHOST", ""):
        raise SystemExit(f"refusing to --apply: PGHOST is not {EXPECTED_PGHOST_MARKER}")
    tokens = load(args.snapshot, {t.lower() for t in args.token})
    pc = prod_conn()
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(tokens)} tokens from {args.snapshot}", flush=True)
    t0 = time.time()
    for token, entry in tokens.items():
        restore_token(pc, token, entry, args.apply)
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
