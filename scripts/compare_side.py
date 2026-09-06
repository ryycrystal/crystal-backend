"""Compare replayed positions in the side database against prod, and against a known answer.

python scripts/compare_side.py --user 0xb9e3... --token 0x405b...
python scripts/compare_side.py --token 0x405b... --top 15
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_loader import load_env  # noqa: E402

load_env()

E = 10**18
COLS = (
    "token_bought",
    "token_sold",
    "native_spent",
    "native_received",
    "cost_basis_native",
    "realized_pnl_native",
    "balance_token",
    "trade_count",
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


def fetch(conn, user, token):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {','.join(COLS)} FROM launchpad_positions WHERE user_address=%s AND token=%s",
            (user, token),
        )
        row = cur.fetchone()
    return dict(zip(COLS, row)) if row else None


def show_pair(sc, pc, user, token):
    s = fetch(sc, user, token)
    p = fetch(pc, user, token)
    print(f"\n{user}  {token}")
    print(f"{'column':<22}{'prod':>18}{'side (replay)':>18}{'diff':>18}")
    for c in COLS:
        pv = float(p[c]) if p and p[c] is not None else 0.0
        sv = float(s[c]) if s and s[c] is not None else 0.0
        scale = 1 if c == "trade_count" else E
        print(f"{c:<22}{pv / scale:>18,.2f}{sv / scale:>18,.2f}{(sv - pv) / scale:>18,.2f}")
    with sc.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM launchpad_trades WHERE user_address=%s AND token=%s", (user, token))
        st = cur.fetchone()[0]
    with pc.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM launchpad_trades WHERE user_address=%s AND token=%s", (user, token))
        pt = cur.fetchone()[0]
    print(f"{'trade rows':<22}{pt:>18,}{st:>18,}{st - pt:>18,}")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user")
    ap.add_argument("--token", required=True)
    ap.add_argument("--top", type=int, default=0, help="also show the N side positions with largest realized pnl")
    ap.add_argument("--expect-bought", type=float)
    ap.add_argument("--expect-spent", type=float)
    ap.add_argument("--expect-realized", type=float)
    args = ap.parse_args()

    sc = side_conn()
    pc = prod_conn()
    pc.set_session(readonly=True, autocommit=True)
    token = args.token.lower()

    ok = True
    if args.user:
        s = show_pair(sc, pc, args.user.lower(), token)
        if s and args.expect_bought is not None:
            checks = [
                ("token_bought", args.expect_bought),
                ("native_spent", args.expect_spent),
                ("realized_pnl_native", args.expect_realized),
            ]
            print("\nknown-answer check:")
            for col, want in checks:
                if want is None:
                    continue
                got = float(s[col]) / E
                good = abs(got - want) <= max(abs(want) * 0.0005, 0.5)
                ok &= good
                print(f"  {col:<20} got {got:>16,.2f}   want {want:>16,.2f}   {'OK' if good else 'MISMATCH'}")

    if args.top:
        with sc.cursor() as cur:
            cur.execute(
                """SELECT user_address FROM launchpad_positions WHERE token=%s
                   ORDER BY realized_pnl_native DESC LIMIT %s""",
                (token, args.top),
            )
            users = [r[0] for r in cur.fetchall()]
        for u in users:
            show_pair(sc, pc, u, token)

    with sc.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(native_amount),0)/1e18 FROM launchpad_trades WHERE token=%s", (token,)
        )
        st, sv = cur.fetchone()
    with pc.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(native_amount),0)/1e18 FROM launchpad_trades WHERE token=%s", (token,)
        )
        pt, pv = cur.fetchone()
    print(f"\ntoken-wide trades: prod {pt:,} ({float(pv):,.0f} MON)   side {st:,} ({float(sv):,.0f} MON)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
