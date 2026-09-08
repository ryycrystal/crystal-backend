"""Verify the ledger against itself and against the chain, for every token it holds.

`ledger_check.py` proves three hand-picked fixtures. This proves properties that must hold for *any* token,
so it stays useful once the replay covers the whole registry:

  invariants  exact SQL identities between wallet_flows and positions_v2, so a fold or write that lost rows
              is visible without touching the chain;
  coverage    tokens whose earliest flow is later than their creation block, which is the signature of a
              replay that began too late and surfaces as negative balances;
  supply      totalSupply == ledger wallets + venues + the token contract + burn addresses, per token, read
              at that token's last folded block. This is the only check that can catch a holder the replay
              never recorded at all, because a per-wallet comparison can only ask about wallets it already
              knows about.

Read-only against the side database and the public RPC.

  python scripts/ledger_verify.py --dsn side_dsn.txt [--token 0x...] [--skip-supply]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ledger.types import INTERPRETATION  # noqa: E402

RPC = os.environ.get("RPC_HTTP", "https://rpc.monad.xyz")
TOTAL_SUPPLY = "0x18160ddd"
BALANCE_OF = "0x70a08231"
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"
PROBE_BATCH = 40

CHECKS = [
    (
        "balance_token equals the sum of that wallet's flow deltas",
        """
        SELECT count(*) FROM positions_v2 p
        WHERE p.balance_token IS DISTINCT FROM (
            SELECT COALESCE(SUM(f.token_delta), 0) FROM wallet_flows f
            WHERE f.token = p.token AND f.wallet = p.wallet
        )
        """,
    ),
    (
        "custody_balance equals the sum of that wallet's custody legs",
        """
        SELECT count(*) FROM positions_v2 p
        WHERE COALESCE(p.custody_balance, 0) IS DISTINCT FROM (
            SELECT COALESCE(SUM(CASE
                WHEN f.kind = 'custody_deposit' THEN abs(f.token_delta)
                WHEN f.kind = 'custody_withdraw' THEN -abs(f.token_delta)
                ELSE 0 END), 0)
            FROM wallet_flows f WHERE f.token = p.token AND f.wallet = p.wallet
        )
        """,
    ),
    (
        "no negative balance among tokens covered from creation",
        """
        SELECT count(*) FROM positions_v2 p
        WHERE p.balance_token < 0
          AND p.token IN (SELECT token FROM ledger_token_coverage WHERE from_creation)
        """,
    ),
    ("no position holds a negative custody balance", "SELECT count(*) FROM positions_v2 WHERE custody_balance < 0"),
    ("no position reports a negative sold quantity", "SELECT count(*) FROM positions_v2 WHERE token_sold < 0"),
    (
        "every wallet with flows in a token covered from creation has a position row",
        """
        SELECT count(*) FROM (
            SELECT DISTINCT token, wallet FROM wallet_flows
            WHERE token IN (SELECT token FROM ledger_token_coverage WHERE from_creation)
            EXCEPT SELECT token, wallet FROM positions_v2
        ) missing
        """,
    ),
    (
        "no position is served for a token whose coverage does not reach its creation",
        """
        SELECT count(*) FROM positions_v2
        WHERE token NOT IN (SELECT token FROM ledger_token_coverage WHERE from_creation)
        """,
    ),
    (
        "every position row has at least one flow",
        """
        SELECT count(*) FROM (
            SELECT token, wallet FROM positions_v2
            EXCEPT SELECT DISTINCT token, wallet FROM wallet_flows
        ) orphan
        """,
    ),
    (
        "no wallet holds two movements of one token at a single chain position",
        """
        SELECT count(*) FROM (
            SELECT block_number, tx_index, log_index, wallet, token, sign(token_delta)
            FROM wallet_flows GROUP BY 1,2,3,4,5,6 HAVING count(*) > 1
        ) dupes
        """,
    ),
    (
        "both halves of a wallet-to-wallet transfer are stored",
        """
        SELECT count(*) FROM wallet_flows f
        JOIN address_kinds k ON k.address = f.counterparty
        WHERE f.kind IN ('transfer_in', 'transfer_out')
          AND k.kind IN ('eoa', 'eoa_7702', 'wallet_4337', 'contract_unknown')
          AND NOT EXISTS (
              SELECT 1 FROM wallet_flows g
              WHERE g.txhash = f.txhash AND g.token = f.token AND g.wallet = f.counterparty
                AND sign(g.token_delta) = -sign(f.token_delta)
          )
        """,
    ),
    (
        "a transfer between two wallets moves its cost as well as its tokens",
        """
        SELECT count(*) FROM (
            SELECT o.txhash, o.token, o.wallet, sum(o.basis_delta) AS released,
                   (SELECT coalesce(sum(i.basis_delta), 0) FROM wallet_flows i
                     WHERE i.txhash = o.txhash AND i.token = o.token AND i.counterparty = o.wallet
                       AND i.token_delta > 0) AS taken
            FROM wallet_flows o
            WHERE o.kind = 'transfer_out' AND o.basis_delta < 0
            GROUP BY o.txhash, o.token, o.wallet
        ) pairs WHERE released <> -taken
        """,
    ),
    (
        "no position sits on an address classified as a venue",
        """
        SELECT count(*) FROM positions_v2 p
        WHERE EXISTS (SELECT 1 FROM address_kinds k WHERE k.address = p.wallet AND k.kind LIKE 'venue%%')
           OR EXISTS (SELECT 1 FROM venues v WHERE v.address = p.wallet)
        """,
    ),
    (
        "no flow claims a quote it did not name an asset for",
        "SELECT count(*) FROM wallet_flows WHERE quote_delta <> 0 AND (quote_asset IS NULL OR quote_asset = '')",
    ),
    (
        "observed basis never rests on a missing or zero quote",
        "SELECT count(*) FROM wallet_flows WHERE basis_state = 'observed' AND kind IN ('buy','sell') "
        "AND COALESCE(quote_delta, 0) = 0",
    ),
    (
        "unresolved flows carry no invented cost",
        "SELECT count(*) FROM wallet_flows WHERE basis_state = 'unresolved' AND COALESCE(quote_delta, 0) <> 0",
    ),
    (
        "an unpriced disposal books no realized result",
        "SELECT count(*) FROM wallet_flows WHERE kind = 'sell' AND COALESCE(quote_delta, 0) = 0 "
        "AND COALESCE(mon_value, 0) = 0 AND (realized_observed_delta <> 0 OR realized_estimated_delta <> 0)",
    ),
    (
        "fold totals agree with the vector they summarize",
        "SELECT count(*) FROM wallet_flows WHERE basis_delta <> basis_observed_delta + basis_estimated_delta "
        "OR realized_delta <> realized_observed_delta + realized_estimated_delta",
    ),
    (
        "every position's open inventory and results are the sums of its flows' effects",
        """
        SELECT count(*) FROM positions_v2 p JOIN (
            SELECT wallet, token,
                   SUM(qty_observed) AS qo, SUM(qty_estimated) AS qe, SUM(qty_unresolved) AS qu,
                   SUM(basis_observed_delta) AS bo, SUM(basis_estimated_delta) AS be,
                   SUM(realized_observed_delta) AS ro, SUM(realized_estimated_delta) AS re,
                   SUM(unresolved_proceeds_delta) AS up, SUM(disposed_unresolved_basis_delta) AS du
            FROM wallet_flows GROUP BY wallet, token
        ) s ON s.wallet = p.wallet AND s.token = p.token
        WHERE s.qo <> p.observed_tokens OR s.qe <> p.estimated_tokens OR s.qu <> p.unresolved_tokens
           OR s.bo <> p.cost_basis_native OR s.be <> p.basis_estimated_native
           OR s.ro <> p.realized_pnl_native OR s.re <> p.realized_estimated_native
           OR s.up <> p.unresolved_proceeds_native OR s.du <> p.disposed_unresolved_basis_native
        """,
    ),
    (
        "every position's parked inventory is the sum of its park and restore flows",
        """
        SELECT count(*) FROM positions_v2 p JOIN (
            SELECT wallet, token,
                   -SUM(qty_observed) AS po, -SUM(qty_estimated) AS pe,
                   -SUM(basis_observed_delta) AS pbo, -SUM(basis_estimated_delta) AS pbe
            FROM wallet_flows WHERE kind IN ('lp_add', 'lp_remove', 'vault_deposit', 'vault_withdraw')
            GROUP BY wallet, token
        ) s ON s.wallet = p.wallet AND s.token = p.token
        WHERE s.po <> p.parked_observed_tokens OR s.pe <> p.parked_estimated_tokens
           OR s.pbo <> p.parked_observed_basis OR s.pbe <> p.parked_estimated_basis
        """,
    ),
    (
        "each position's parked totals are the sum of what the individual pools and vaults hold",
        """
        SELECT count(*) FROM positions_v2 p
        LEFT JOIN (
            SELECT wallet, token, SUM(observed_tokens) AS ot, SUM(estimated_tokens) AS et,
                   SUM(unresolved_tokens) AS ut, SUM(observed_basis) AS ob, SUM(estimated_basis) AS eb
            FROM parked_entitlements GROUP BY wallet, token
        ) e ON e.wallet = p.wallet AND e.token = p.token
        WHERE COALESCE(e.ot, 0) <> p.parked_observed_tokens OR COALESCE(e.et, 0) <> p.parked_estimated_tokens
           OR COALESCE(e.ut, 0) <> p.parked_unresolved_tokens OR COALESCE(e.ob, 0) <> p.parked_observed_basis
           OR COALESCE(e.eb, 0) <> p.parked_estimated_basis
        """,
    ),
    (
        "no pool or vault holds basis for tokens it does not hold",
        "SELECT count(*) FROM parked_entitlements WHERE observed_tokens < 0 OR estimated_tokens < 0 "
        "OR unresolved_tokens < 0 OR observed_basis < 0 OR estimated_basis < 0 "
        "OR (observed_tokens = 0 AND observed_basis <> 0) OR (estimated_tokens = 0 AND estimated_basis <> 0)",
    ),
    (
        "no wallet holds two swap legs of one token against each other",
        """
        SELECT count(*) FROM wallet_flows a JOIN wallet_flows b
          ON a.txhash = b.txhash AND a.wallet = b.wallet AND a.token = b.token AND a.log_index <> b.log_index
        WHERE a.kind = 'swap_leg' AND b.kind = 'swap_leg' AND sign(a.token_delta) <> sign(b.token_delta)
        """,
    ),
    (
        "every flow was written by the current interpretation",
        f"SELECT count(*) FROM wallet_flows WHERE interpretation < {INTERPRETATION}",
    ),
    (
        "no position carries negative inventory or negative held-out basis",
        "SELECT count(*) FROM positions_v2 WHERE observed_tokens < 0 OR estimated_tokens < 0 "
        "OR unresolved_tokens < 0 OR cost_basis_native < 0 OR basis_estimated_native < 0 "
        "OR parked_observed_basis < 0 OR parked_estimated_basis < 0 "
        "OR disposed_unresolved_tokens < 0 OR disposed_unresolved_basis_native < 0",
    ),
]

COVERAGE = """
    SELECT count(*) FROM (
        SELECT DISTINCT token FROM wallet_flows
        EXCEPT SELECT token FROM ledger_token_coverage WHERE from_creation
    ) partial
"""

FULLY_REPLAYED = """
    SELECT DISTINCT f.token FROM wallet_flows f
    WHERE f.token IN (SELECT token FROM ledger_token_coverage WHERE from_creation)
"""

NEGATIVES = "SELECT count(*) FROM positions_v2 WHERE balance_token < 0"

STALE_INTERPRETATIONS = "SELECT count(*) FROM wallet_flows WHERE interpretation < %s"


def rpc_batch(calls: list[tuple[str, str, str]], attempts: int = 6) -> list:
    """calls are (to, data, block); returns hex results, retrying only what did not answer."""
    out: list = [None] * len(calls)
    pending = list(range(len(calls)))
    delay = 1.0
    for _ in range(attempts):
        if not pending:
            break
        for start in range(0, len(pending), PROBE_BATCH):
            chunk = pending[start : start + PROBE_BATCH]
            payload = [
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "eth_call",
                    "params": [{"to": calls[i][0], "data": calls[i][1]}, calls[i][2]],
                }
                for i in chunk
            ]
            req = urllib.request.Request(
                RPC, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    replies = json.loads(resp.read())
                if isinstance(replies, dict):
                    replies = [replies]
                for reply in replies:
                    if isinstance(reply, dict) and isinstance(reply.get("result"), str):
                        out[reply["id"]] = reply["result"]
            except Exception:
                pass
            time.sleep(0.4)
        pending = [i for i in pending if out[i] is None]
        if pending:
            time.sleep(delay)
            delay = min(delay * 2, 20)
    return out


def as_int(raw) -> int:
    return int(raw, 16) if raw and raw != "0x" else 0


def run_invariants(cur) -> int:
    failures = 0
    for name, sql in CHECKS:
        cur.execute(sql)
        bad = int(cur.fetchone()[0])
        failures += 1 if bad else 0
        detail = f"  ({bad:,} rows)" if bad else ""
        print(f"  {'PASS' if not bad else 'FAIL'}  {name}{detail}")
    print(f"  {len(CHECKS) - failures}/{len(CHECKS)} invariants hold")
    return failures


PRICE_SPREAD = """
WITH priced AS (
    SELECT price_native FROM wallet_flows
    WHERE token = %s AND basis_state = 'observed' AND kind IN ('buy', 'sell')
      AND price_native IS NOT NULL AND price_native > 0
),
middle AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price_native) m FROM priced)
SELECT count(*) FILTER (WHERE price_native > m * %s OR price_native < m / %s), count(*),
       min(price_native), (SELECT m FROM middle), max(price_native)
FROM priced, middle
"""
SPREAD_FACTOR = 1000


def run_prices(cur, tokens: list[str]) -> None:
    """How far observed prices sit from a token's own median.

    Reported rather than asserted: a token's price genuinely moves over its life, so a wide spread is not by
    itself wrong. It is the shape a decimals or units error makes, and it is worth looking at before
    trusting any cost figure, because a single absurd price also poisons every flow later priced by
    reference to it.
    """
    for token in tokens:
        cur.execute(PRICE_SPREAD, (token, SPREAD_FACTOR, SPREAD_FACTOR))
        off, priced, low, mid, high = cur.fetchone()
        if not priced:
            continue
        print(
            f"  {token[:12]}: {off:,} of {priced:,} observed prices are more than {SPREAD_FACTOR}x from the "
            f"median  [{float(low):.3e} .. {float(mid):.3e} .. {float(high):.3e} MON per token]"
        )


def run_supply(cur, tokens: list[str]) -> int:
    failures = 0
    for token in tokens:
        cur.execute("SELECT MAX(block_number) FROM wallet_flows WHERE token = %s", (token,))
        row = cur.fetchone()
        if not row or not row[0]:
            continue
        head = int(row[0])
        tag = hex(head)
        cur.execute(
            "SELECT COALESCE(SUM(balance_token), 0) + COALESCE(SUM(custody_balance), 0) "
            "FROM positions_v2 WHERE token = %s",
            (token,),
        )
        ledger_held = int(cur.fetchone()[0] or 0)
        cur.execute(
            """
            SELECT DISTINCT f.counterparty FROM wallet_flows f
            WHERE f.token = %s AND f.counterparty IS NOT NULL AND f.counterparty <> ''
              AND (EXISTS (SELECT 1 FROM venues v WHERE v.address = f.counterparty)
                   OR EXISTS (SELECT 1 FROM address_kinds k WHERE k.address = f.counterparty
                              AND k.kind LIKE 'venue%%'))
            """,
            (token,),
        )
        venues = sorted({r[0] for r in cur.fetchall() if r[0]})
        others = [a for a in (token, ZERO, DEAD) if a not in venues]
        probes = venues + others
        calls = [(token, TOTAL_SUPPLY, tag)]
        calls += [(token, BALANCE_OF + a.removeprefix("0x").rjust(64, "0"), tag) for a in probes]
        results = rpc_batch(calls)
        unanswered = sum(1 for r in results if r is None)
        cur.execute("SELECT wallet FROM positions_v2 WHERE token = %s AND wallet = ANY(%s)", (token, probes))
        already = {r[0] for r in cur.fetchall()}
        supply = as_int(results[0])
        held = sum(as_int(results[1 + i]) for i, a in enumerate(probes) if a not in already)
        residual = supply - (ledger_held + held)
        ok = residual == 0 and not unanswered and supply > 0
        failures += 0 if ok else 1
        pct = (abs(residual) / supply * 100) if supply else 0
        print(
            f"  {'PASS' if ok else 'FAIL'}  {token[:12]} at {head:,}: unaccounted {residual / 1e18:,.6f} "
            f"({pct:.6f}% of supply), {len(venues)} venues, {unanswered} unanswered"
        )
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True, help="file holding the side database DSN")
    ap.add_argument("--token", action="append", default=[], help="restrict the supply check to these tokens")
    ap.add_argument("--skip-supply", action="store_true", help="run only the SQL checks, no chain reads")
    args = ap.parse_args()

    conn = psycopg2.connect(open(args.dsn).read().strip())
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT count(*), count(DISTINCT token) FROM positions_v2")
    positions, held_tokens = cur.fetchone()
    cur.execute("SELECT count(*) FROM wallet_flows")
    flows = cur.fetchone()[0]
    print(f"{positions:,} positions and {flows:,} flows across {held_tokens} tokens")
    print("")
    print("invariants")
    failures = run_invariants(cur)

    cur.execute(COVERAGE)
    partial = int(cur.fetchone()[0])
    cur.execute(NEGATIVES)
    negatives = int(cur.fetchone()[0])
    cur.execute(STALE_INTERPRETATIONS, (INTERPRETATION,))
    stale = int(cur.fetchone()[0])
    print("")
    print("coverage")
    print(f"  {partial} token(s) hold flows without coverage from creation; {negatives} negative balance(s) overall")
    print(f"  {stale:,} flow(s) were written by an interpretation older than {INTERPRETATION}")

    tokens = [t.lower() for t in args.token]
    if not tokens:
        cur.execute(FULLY_REPLAYED)
        tokens = [r[0] for r in cur.fetchall()]
    print("")
    print("observed price spread")
    run_prices(cur, tokens)

    if not args.skip_supply:
        print("")
        print(f"supply conservation ({len(tokens)} fully replayed token(s))")
        failures += run_supply(cur, tokens)

    print("")
    print("OK" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
