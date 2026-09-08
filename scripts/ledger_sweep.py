"""Interrogate one rebuilt token for the defect classes a quantity check cannot see, and grade it.

Every defect found in this work came from asking the stored rows a question the tests had not thought to
ask, so this runs the whole set at once. It ends with the acceptance the product owner set: fewer than one
position in ten carries any estimated cost, no such position is more than a tenth estimated, and no
unclassified contract sits among a token's top holders without being listed for review.

    DATABASE_URL=... python scripts/ledger_sweep.py 0xTOKEN [--dsn path]
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ledger.types import INTERPRETATION  # noqa: E402

AFFECTED_SHARE_LIMIT = Decimal("0.10")
ESTIMATED_SHARE_LIMIT = Decimal("0.10")
TOP_HOLDERS = 50

CHECKS = [
    ("negative balance", "SELECT count(*) FROM positions_v2 WHERE token=%(t)s AND balance_token<0"),
    (
        "negative inventory, basis or held-out basis",
        "SELECT count(*) FROM positions_v2 WHERE token=%(t)s AND (observed_tokens<0 OR estimated_tokens<0"
        " OR unresolved_tokens<0 OR cost_basis_native<0 OR basis_estimated_native<0"
        " OR disposed_unresolved_basis_native<0 OR parked_observed_basis<0)",
    ),
    (
        "position inventory is not the sum of its flows' effects",
        """SELECT count(*) FROM positions_v2 p JOIN (
             SELECT wallet, SUM(qty_observed) qo, SUM(qty_estimated) qe, SUM(qty_unresolved) qu,
                    SUM(basis_observed_delta) bo, SUM(basis_estimated_delta) be, SUM(realized_observed_delta) ro,
                    SUM(realized_estimated_delta) re, SUM(unresolved_proceeds_delta) up,
                    SUM(disposed_unresolved_basis_delta) du
             FROM wallet_flows WHERE token=%(t)s GROUP BY wallet) s ON s.wallet=p.wallet
           WHERE p.token=%(t)s AND (s.qo<>p.observed_tokens OR s.qe<>p.estimated_tokens OR s.qu<>p.unresolved_tokens
             OR s.bo<>p.cost_basis_native OR s.be<>p.basis_estimated_native OR s.ro<>p.realized_pnl_native
             OR s.re<>p.realized_estimated_native OR s.up<>p.unresolved_proceeds_native
             OR s.du<>p.disposed_unresolved_basis_native)""",
    ),
    (
        "fold totals disagree with the vector they summarize",
        "SELECT count(*) FROM wallet_flows WHERE token=%(t)s AND (basis_delta<>basis_observed_delta+"
        "basis_estimated_delta OR realized_delta<>realized_observed_delta+realized_estimated_delta)",
    ),
    (
        "observed basis resting on a missing quote",
        "SELECT count(*) FROM wallet_flows WHERE token=%(t)s AND basis_state='observed'"
        " AND kind IN ('buy','sell') AND COALESCE(quote_delta,0)=0",
    ),
    (
        "unresolved flow carrying an invented cost",
        "SELECT count(*) FROM wallet_flows WHERE token=%(t)s AND basis_state='unresolved'"
        " AND COALESCE(quote_delta,0)<>0",
    ),
    (
        "unpriced sale booking a realized result",
        "SELECT count(*) FROM wallet_flows WHERE token=%(t)s AND kind='sell' AND COALESCE(quote_delta,0)=0"
        " AND COALESCE(mon_value,0)=0 AND (realized_observed_delta<>0 OR realized_estimated_delta<>0)",
    ),
    (
        "position on an address classified as a venue",
        "SELECT count(*) FROM positions_v2 p WHERE p.token=%(t)s AND EXISTS"
        " (SELECT 1 FROM address_kinds k WHERE k.address=p.wallet AND k.kind LIKE 'venue%%')",
    ),
    (
        "one wallet with two movements of one token, one side, one chain position",
        "SELECT count(*) FROM (SELECT block_number,tx_index,log_index,sub_index"
        " FROM wallet_flows WHERE token=%(t)s GROUP BY 1,2,3,4 HAVING count(*)>1) d",
    ),
    (
        "wallet-to-wallet transfer missing its other half",
        """WITH halves AS (
             SELECT DISTINCT txhash, token, wallet, sign(token_delta) AS side FROM wallet_flows WHERE token=%(t)s)
           SELECT count(*) FROM wallet_flows f JOIN address_kinds k ON k.address=f.counterparty
           LEFT JOIN halves g ON g.txhash=f.txhash AND g.token=f.token AND g.wallet=f.counterparty
             AND g.side=-sign(f.token_delta)
           WHERE f.token=%(t)s AND f.kind IN ('transfer_in','transfer_out')
             AND k.kind IN ('eoa','eoa_7702','wallet_4337','contract_unknown') AND g.wallet IS NULL""",
    ),
    (
        "transfer where the cost did not travel with the tokens",
        """WITH released AS (
             SELECT txhash, token, wallet AS sender, sum(basis_delta) AS released
             FROM wallet_flows WHERE token=%(t)s AND kind='transfer_out' AND basis_delta<0
             GROUP BY txhash, token, wallet
           ), taken AS (
             SELECT txhash, token, counterparty AS sender, sum(basis_delta) AS taken
             FROM wallet_flows WHERE token=%(t)s AND token_delta>0 AND counterparty IS NOT NULL
             GROUP BY txhash, token, counterparty
           )
           SELECT count(*) FROM released r
           LEFT JOIN taken t ON t.txhash=r.txhash AND t.token=r.token AND t.sender=r.sender
           WHERE r.released <> -coalesce(t.taken, 0)""",
    ),
    (
        "parked totals disagree with the per-venue rows",
        """SELECT count(*) FROM positions_v2 p LEFT JOIN (
             SELECT wallet, SUM(observed_tokens) ot, SUM(observed_basis) ob, SUM(estimated_tokens) et
             FROM parked_entitlements WHERE token=%(t)s GROUP BY wallet) e ON e.wallet=p.wallet
           WHERE p.token=%(t)s AND (COALESCE(e.ot,0)<>p.parked_observed_tokens
             OR COALESCE(e.ob,0)<>p.parked_observed_basis OR COALESCE(e.et,0)<>p.parked_estimated_tokens)""",
    ),
    (
        "a swap leg paired with a movement of the same token",
        """SELECT count(*) FROM wallet_flows a JOIN wallet_flows b
           ON a.txhash=b.txhash AND a.wallet=b.wallet AND a.token=b.token AND a.log_index<>b.log_index
           WHERE a.token=%(t)s AND a.kind='swap_leg' AND b.kind='swap_leg'
             AND sign(a.token_delta)<>sign(b.token_delta)""",
    ),
    (
        "flow written by an older interpretation",
        "SELECT count(*) FROM wallet_flows WHERE token=%(t)s AND interpretation<%(v)s",
    ),
    (
        "token served as positions without coverage from creation",
        "SELECT count(*) FROM positions_v2 WHERE token=%(t)s AND token NOT IN"
        " (SELECT token FROM ledger_token_coverage WHERE from_creation)",
    ),
]

OUTLIERS = """
WITH priced AS (
    SELECT block_number, price_native FROM wallet_flows
    WHERE token=%(t)s AND basis_state='observed' AND kind IN ('buy','sell')
      AND price_native IS NOT NULL AND price_native > 0
),
middle AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price_native) m FROM priced)
SELECT count(*) FILTER (WHERE price_native > m * 1000 OR price_native < m / 1000), count(*),
       min(price_native), (SELECT m FROM middle), max(price_native)
FROM priced, middle
"""

SHARES = """
SELECT COALESCE(SUM(token_delta) FILTER (WHERE token_delta>0),0),
       COALESCE(SUM(token_delta) FILTER (WHERE token_delta>0 AND basis_state='unresolved'),0),
       COALESCE(SUM(qty_unresolved) FILTER (WHERE token_delta>0),0)
FROM wallet_flows WHERE token=%(t)s
"""

ACCEPTANCE = """
WITH holders AS (
    SELECT p.wallet, p.cost_basis_native, p.basis_estimated_native, p.realized_pnl_native, p.realized_estimated_native
    FROM positions_v2 p
    WHERE p.token=%(t)s AND EXISTS (SELECT 1 FROM address_kinds k WHERE k.address=p.wallet
                                    AND k.kind IN ('eoa','eoa_7702','wallet_4337'))
), scored AS (
    SELECT wallet,
           CASE WHEN cost_basis_native + basis_estimated_native > 0
                THEN basis_estimated_native::numeric / (cost_basis_native + basis_estimated_native)
                WHEN abs(realized_pnl_native) + abs(realized_estimated_native) > 0
                THEN abs(realized_estimated_native)::numeric / (abs(realized_pnl_native) + abs(realized_estimated_native))
                ELSE NULL END AS estimated_share
    FROM holders
)
SELECT count(*) FILTER (WHERE estimated_share IS NOT NULL),
       count(*) FILTER (WHERE estimated_share > 0),
       count(*) FILTER (WHERE estimated_share > %(limit)s)
FROM scored
"""

TOP_UNKNOWN = """
SELECT p.wallet, p.balance_token / 1e18,
       (SELECT count(DISTINCT f.wallet) FROM wallet_flows f WHERE f.counterparty = p.wallet AND f.token_delta > 0)
FROM (SELECT wallet, balance_token FROM positions_v2 WHERE token=%(t)s ORDER BY balance_token DESC LIMIT %(top)s) p
JOIN address_kinds k ON k.address = p.wallet
WHERE k.kind = 'contract_unknown'
ORDER BY p.balance_token DESC
"""


def dsn_from(args: argparse.Namespace) -> str:
    if args.dsn:
        return open(args.dsn, encoding="utf-8").read().strip()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("set DATABASE_URL or pass --dsn")
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("token")
    ap.add_argument("--dsn", help="file holding the side database url; DATABASE_URL otherwise")
    args = ap.parse_args()
    token = args.token.lower()
    conn = psycopg2.connect(dsn_from(args))
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    params = {"t": token, "v": INTERPRETATION, "limit": ESTIMATED_SHARE_LIMIT, "top": TOP_HOLDERS}
    failures = 0
    for name, sql in CHECKS:
        cur.execute(sql, params)
        bad = int(cur.fetchone()[0])
        failures += 1 if bad else 0
        print(f"  {'PASS' if not bad else 'FAIL'}  {name}" + (f"  ({bad:,})" if bad else ""))
    cur.execute("SELECT count(*), count(DISTINCT wallet) FROM wallet_flows WHERE token=%(t)s", params)
    flows, wallets = cur.fetchone()
    cur.execute(OUTLIERS, params)
    off, priced, low, mid, high = cur.fetchone()
    if priced:
        verdict = "PASS" if not off else "FAIL"
        print(f"  {verdict}  observed prices within a thousandfold of the median  ({off:,} of {priced:,} outside)")
        print(f"        price range {low:.3e} .. median {mid:.3e} .. {high:.3e} MON per token")
        failures += 1 if off else 0
    cur.execute(SHARES, params)
    inflow, unpriced, without_cost = cur.fetchone()
    print(f"\n  {flows:,} flows across {wallets:,} wallets")
    if inflow:
        print(f"  inflow arriving with no price of its own : {unpriced / inflow * 100:6.3f}%")
        print(f"  inflow that still has no cost at all     : {without_cost / inflow * 100:6.3f}%")

    cur.execute(ACCEPTANCE, params)
    scored, affected, over = cur.fetchone()
    if scored:
        share = Decimal(affected) / Decimal(scored)
        ok_share = share <= AFFECTED_SHARE_LIMIT
        print(
            f"\n  {'PASS' if ok_share else 'FAIL'}  positions carrying any estimated cost: {affected:,} of {scored:,}"
            f" ({share * 100:.2f}%, limit {AFFECTED_SHARE_LIMIT * 100:.0f}%)"
        )
        print(
            f"  {'PASS' if not over else 'FAIL'}  positions more than {ESTIMATED_SHARE_LIMIT * 100:.0f}% estimated: {over:,}"
        )
        failures += (0 if ok_share else 1) + (1 if over else 0)
    cur.execute(TOP_UNKNOWN, params)
    unknown = cur.fetchall()
    if unknown:
        print(f"\n  REVIEW  unclassified contracts among the top {TOP_HOLDERS} holders:")
        for addr, balance, served in unknown:
            print(f"        {addr}  {float(balance):>18,.2f} tokens  passed tokens to {served:,} wallets")
    else:
        print(f"\n  PASS  no unclassified contract among the top {TOP_HOLDERS} holders")
    total = len(CHECKS) + (1 if priced else 0) + (2 if scored else 0)
    print(f"\n{total - failures}/{total} hold for {token}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
