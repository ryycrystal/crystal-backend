"""Quantify what merging the side database would change on prod, per token and overall.

Read-only on both sides.

    DATABASE_URL=<side> PROD_PGHOSTADDR=127.0.0.1 PROD_PGPORT=15433 \\
      python scripts/report_side.py --tokens-file worker0_tokens.json [--top 5] [--json report.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_loader import load_env  # noqa: E402

load_env()

E = Decimal(10) ** 18
COLS = ("token_bought", "token_sold", "native_spent", "native_received", "cost_basis_native", "realized_pnl_native")


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


def positions(conn, token):
    with conn.cursor() as cur:
        cur.execute(f"SELECT user_address,{','.join(COLS)} FROM launchpad_positions WHERE token=%s", (token,))
        return {r[0]: {c: Decimal(v or 0) for c, v in zip(COLS, r[1:])} for r in cur.fetchall()}


def trade_keys(conn, token):
    with conn.cursor() as cur:
        cur.execute("SELECT LOWER(txhash), log_index FROM launchpad_trades WHERE token=%s", (token,))
        return {(t, int(i)) for t, i in cur.fetchall()}


def report_token(sc, pc, token, top):
    side, prod = positions(sc, token), positions(pc, token)
    st, pt = trade_keys(sc, token), trade_keys(pc, token)
    changed, inserts, deltas = 0, 0, []
    for user, s in side.items():
        p = prod.get(user)
        if p is None:
            inserts += 1
            deltas.append((abs(s["realized_pnl_native"]), user, Decimal(0), s["realized_pnl_native"], "new"))
            continue
        if any(abs(s[c] - p[c]) > 1 for c in COLS):
            changed += 1
            deltas.append(
                (
                    abs(s["realized_pnl_native"] - p["realized_pnl_native"]),
                    user,
                    p["realized_pnl_native"],
                    s["realized_pnl_native"],
                    "changed",
                )
            )
    realized_delta = sum((d[3] - d[2] for d in deltas), Decimal(0))
    row = {
        "token": token,
        "prod_positions": len(prod),
        "side_positions": len(side),
        "changed": changed,
        "inserted": inserts,
        "prod_trades": len(pt),
        "side_trades": len(st),
        "new_trades": len(st - pt),
        "dropped_trades": len(pt - st),
        "realized_delta_mon": float(realized_delta / E),
    }
    print(
        f"{token}  positions {len(prod):,} -> changed {changed:,} +{inserts:,} new   "
        f"trades {len(pt):,} -> {len(st):,} (+{len(st - pt):,}, dropped {len(pt - st):,})   "
        f"sum realized delta {realized_delta / E:,.2f} MON",
        flush=True,
    )
    if row["dropped_trades"]:
        print("   WARNING: prod trades missing from side, the merge will skip this token", flush=True)
    for mag, user, before, after, kind in sorted(deltas, reverse=True)[:top]:
        print(f"   {user}  {kind:<7} realized {before / E:>16,.2f} -> {after / E:>16,.2f}", flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", action="append", default=[])
    ap.add_argument("--tokens-file")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--json")
    args = ap.parse_args()

    tokens = [t.lower() for t in args.token]
    if args.tokens_file:
        tokens += [t.lower() for t in json.load(open(args.tokens_file))]
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        raise SystemExit("pass --token or --tokens-file")
    if "crystal_replay" not in os.environ.get("DATABASE_URL", ""):
        raise SystemExit("DATABASE_URL must point at the side database")

    sc, pc = side_conn(), prod_conn()
    pc.set_session(readonly=True, autocommit=True)
    rows = [report_token(sc, pc, t, args.top) for t in tokens]
    total = {
        k: sum(r[k] for r in rows)
        for k in (
            "prod_positions",
            "changed",
            "inserted",
            "prod_trades",
            "new_trades",
            "dropped_trades",
            "realized_delta_mon",
        )
    }
    print(
        f"\nTOTAL {len(rows)} tokens: positions {total['prod_positions']:,} -> changed {total['changed']:,} "
        f"+{total['inserted']:,} new   trades {total['prod_trades']:,} +{total['new_trades']:,} "
        f"(dropped {total['dropped_trades']:,})   sum realized delta {total['realized_delta_mon']:,.2f} MON",
        flush=True,
    )
    if args.json:
        json.dump({"tokens": rows, "total": total}, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()
