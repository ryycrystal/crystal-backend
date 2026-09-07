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
        "no negative balance among tokens replayed from creation",
        """
        SELECT count(*) FROM positions_v2 p
        WHERE p.balance_token < 0 AND p.token IN (
            SELECT f.token FROM wallet_flows f JOIN launchpad_tokens t ON t.token = f.token
            GROUP BY f.token, t.created_block HAVING MIN(f.block_number) <= t.created_block
        )
        """,
    ),
    ("no position holds a negative custody balance", "SELECT count(*) FROM positions_v2 WHERE custody_balance < 0"),
    ("no position reports a negative sold quantity", "SELECT count(*) FROM positions_v2 WHERE token_sold < 0"),
    (
        "every wallet that has flows has a position row",
        """
        SELECT count(*) FROM (
            SELECT DISTINCT token, wallet FROM wallet_flows
            EXCEPT SELECT token, wallet FROM positions_v2
        ) missing
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
        "no duplicate flow keys",
        """
        SELECT count(*) FROM (
            SELECT txhash, log_index, sub_index, wallet, token, count(*)
            FROM wallet_flows GROUP BY 1,2,3,4,5 HAVING count(*) > 1
        ) dupes
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
        "observed basis never rests on a zero quote",
        "SELECT count(*) FROM wallet_flows WHERE basis_state = 'observed' AND kind IN ('buy','sell') AND quote_delta = 0",
    ),
    (
        "unresolved flows carry no invented cost",
        "SELECT count(*) FROM wallet_flows WHERE basis_state = 'unresolved' AND quote_delta <> 0",
    ),
]

COVERAGE = """
    SELECT count(*) FROM (
        SELECT f.token FROM wallet_flows f JOIN launchpad_tokens t ON t.token = f.token
        GROUP BY f.token, t.created_block HAVING MIN(f.block_number) > t.created_block
    ) partial
"""

FULLY_REPLAYED = """
    SELECT f.token FROM wallet_flows f JOIN launchpad_tokens t ON t.token = f.token
    GROUP BY f.token, t.created_block HAVING MIN(f.block_number) <= t.created_block
"""

NEGATIVES = "SELECT count(*) FROM positions_v2 WHERE balance_token < 0"


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
    print("")
    print("coverage")
    print(f"  {partial} token(s) start after their creation block, holding {negatives} negative balance(s)")

    if not args.skip_supply:
        tokens = [t.lower() for t in args.token]
        if not tokens:
            cur.execute(FULLY_REPLAYED)
            tokens = [r[0] for r in cur.fetchall()]
        print("")
        print(f"supply conservation ({len(tokens)} fully replayed token(s))")
        failures += run_supply(cur, tokens)

    print("")
    print("OK" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
