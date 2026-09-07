"""Check the position ledger in the side database against the known fixtures.

Prints a PASS/FAIL table and exits 1 on any failure. The JAMES chain check reads
balanceOf per wallet at 'latest'; the replay's head block and 'latest' can differ by a
few blocks, so a mismatching wallet is re-read from positions_v2 and re-called once.

    DATABASE_URL=postgresql://...@localhost/crystal_ledger python scripts/ledger_check.py [--skip-chain]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WEI = Decimal(10**18)
DUST_WEI = 10**15
VENUE_KINDS = {"venue_pool", "venue_router", "venue_curve", "venue_custody", "token", "zero"}
BALANCE_OF_SELECTOR = "0x70a08231"
CHAIN_BATCH = 20
HOLDER_DUST_WEI = 10**15
RPC_INTERVAL = 0.06

CHIPOTLE = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
CHIPOTLE_WALLET = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
MONCOCK = "0x405b6330e213ded490240cbcdd64790806827777"
MONCOCK_WALLET = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
JAMES = "0x43cf5407bda1400498b8064d50a7e17528d87777"
FIXTURE_TOKENS = (CHIPOTLE, MONCOCK, JAMES)
FIXTURES = {"chipotle": CHIPOTLE, "moncock": MONCOCK, "james": JAMES}

POSITION_COLUMNS = (
    "wallet",
    "token",
    "balance_token",
    "custody_balance",
    "token_bought",
    "token_sold",
    "native_spent",
    "native_received",
    "cost_basis_native",
    "realized_pnl_native",
    "basis_estimated_native",
    "realized_estimated_native",
    "unresolved_tokens",
    "unresolved_proceeds_native",
    "trade_count",
    "buy_count",
    "sell_count",
)


@dataclass(frozen=True)
class Check:
    fixture: str
    name: str
    expected: str
    actual: str
    ok: bool


def from_wei(value) -> Decimal:
    return Decimal(int(value or 0)) / WEI


def within_abs(actual, expected, tolerance) -> bool:
    return abs(Decimal(actual) - Decimal(expected)) <= Decimal(tolerance)


def within_pct(actual, expected, pct) -> bool:
    expected = Decimal(expected)
    if expected == 0:
        return Decimal(actual) == 0
    return abs(Decimal(actual) - expected) <= abs(expected) * Decimal(pct) / Decimal(100)


def check_abs(fixture: str, name: str, actual_wei, expected_units, tolerance_units) -> Check:
    actual = from_wei(actual_wei)
    return Check(
        fixture,
        name,
        f"{Decimal(expected_units)} +- {Decimal(tolerance_units)}",
        f"{actual:.6f}",
        within_abs(actual, expected_units, tolerance_units),
    )


def check_pct(fixture: str, name: str, actual_wei, expected_units, pct) -> Check:
    actual = from_wei(actual_wei)
    return Check(
        fixture,
        name,
        f"{Decimal(expected_units)} +- {Decimal(pct)}%",
        f"{actual:.6f}",
        within_pct(actual, expected_units, pct),
    )


def check_eq(fixture: str, name: str, actual, expected) -> Check:
    return Check(fixture, name, str(expected), str(actual), actual == expected)


def check_le(fixture: str, name: str, actual, limit) -> Check:
    return Check(fixture, name, f"<= {limit}", str(actual), Decimal(actual) <= Decimal(limit))


def dust_limit(token_bought) -> int:
    return max(DUST_WEI, int(token_bought or 0) // 10**9)


def check_missing(fixture: str, name: str) -> Check:
    return Check(fixture, name, "a row", "missing", False)


def all_pass(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def render_table(checks: list[Check]) -> str:
    rows = [("fixture", "check", "expected", "actual", "result")]
    rows += [(c.fixture, c.name, c.expected, c.actual, "PASS" if c.ok else "FAIL") for c in checks]
    widths = [max(len(str(r[i])) for r in rows) for i in range(5)]
    lines = [" | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def estimated_share(rows: list[dict]) -> Decimal | None:
    observed = sum(int(r.get("cost_basis_native") or 0) for r in rows)
    estimated = sum(int(r.get("basis_estimated_native") or 0) for r in rows)
    total = observed + estimated
    if total <= 0:
        return None
    return Decimal(estimated) / Decimal(total)


def unresolved_share(rows: list[dict]) -> Decimal | None:
    unresolved = sum(int(r.get("unresolved_tokens") or 0) for r in rows)
    held_or_sold = sum(
        int(r.get("balance_token") or 0) + int(r.get("custody_balance") or 0) + int(r.get("token_sold") or 0)
        for r in rows
    )
    total = held_or_sold + unresolved
    if total <= 0:
        return None
    return Decimal(unresolved) / Decimal(total)


def position_rows(cur, token: str) -> list[dict]:
    cur.execute(f"SELECT {', '.join(POSITION_COLUMNS)} FROM positions_v2 WHERE token = %s", (token,))
    return [dict(zip(POSITION_COLUMNS, row)) for row in cur.fetchall()]


def flow_shares(cur, token: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Estimated value, tokens arriving with no price of their own, and tokens still without a cost.

    The last two used to be one number. A transfer has no price of its own and its row still says so,
    but the tokens it delivers now carry the sender's cost, so the label no longer measures what it was
    being read as. The share that matters is how much of the inflow ended up in the unresolved bucket,
    and both are reported so the difference between them is visible rather than assumed.
    """
    cur.execute(
        """
        SELECT
            COALESCE(SUM(mon_value) FILTER (WHERE kind IN ('buy', 'sell') AND basis_state = 'estimated'), 0),
            COALESCE(SUM(mon_value) FILTER (WHERE kind IN ('buy', 'sell')), 0),
            COALESCE(SUM(token_delta) FILTER (WHERE token_delta > 0 AND basis_state = 'unresolved'), 0),
            COALESCE(SUM(token_delta) FILTER (WHERE token_delta > 0), 0),
            COALESCE(SUM(qty_unresolved) FILTER (WHERE token_delta > 0), 0)
        FROM wallet_flows WHERE token = %s
        """,
        (token,),
    )
    est, total, unpriced, inflow, without_cost = cur.fetchone()
    est_share = Decimal(est) / Decimal(total) if total and Decimal(total) > 0 else None
    share = (lambda n: Decimal(n) / Decimal(inflow)) if inflow and Decimal(inflow) > 0 else (lambda n: None)
    return est_share, share(without_cost), share(unpriced)


def token_shares(cur, tokens: list[str]) -> dict[str, tuple]:
    return {token: flow_shares(cur, token) for token in tokens}


def position(cur, wallet: str, token: str) -> dict | None:
    cur.execute(
        f"SELECT {', '.join(POSITION_COLUMNS)} FROM positions_v2 WHERE wallet = %s AND token = %s",
        (wallet, token),
    )
    row = cur.fetchone()
    return dict(zip(POSITION_COLUMNS, row)) if row else None


def trade_basis_states(cur, wallet: str, token: str) -> dict[str, int]:
    cur.execute(
        """
        SELECT basis_state, count(*) FROM wallet_flows
        WHERE wallet = %s AND token = %s AND kind IN ('buy', 'sell')
        GROUP BY basis_state
        """,
        (wallet, token),
    )
    return {state: int(n) for state, n in cur.fetchall()}


def chipotle_checks(cur) -> list[Check]:
    """CHIPOTLE against hand-derived figures.

    native_spent and realized moved on 2026-09-07, when router fees stopped being removed from cost. The
    earlier figures booked the venue's amount; these book what the wallet actually sent, verified against
    tx.value for every one of its buys. The two differ by 166.546970 MON of fees, and realized falls by
    exactly that, because proceeds did not change.
    """
    name = "CHIPOTLE"
    pos = position(cur, CHIPOTLE_WALLET, CHIPOTLE)
    if pos is None:
        return [check_missing(name, "position row")]
    states = trade_basis_states(cur, CHIPOTLE_WALLET, CHIPOTLE)
    non_observed = sum(n for state, n in states.items() if state != "observed")
    return [
        check_eq(name, "trade_count", int(pos["trade_count"]), 20),
        check_abs(name, "token_bought", pos["token_bought"], "3173915918.78", "0.01"),
        check_abs(name, "token_sold", pos["token_sold"], "3173915918.78", "0.01"),
        check_abs(name, "native_spent", pos["native_spent"], "20890.627", "0.01"),
        check_abs(name, "native_received", pos["native_received"], "34574.254", "0.01"),
        check_abs(name, "realized_pnl_native", pos["realized_pnl_native"], "13683.627", "0.01"),
        Check(name, "trade flows observed", "all observed", json.dumps(states, sort_keys=True), non_observed == 0),
        check_le(name, "balance_token", int(pos["balance_token"]), dust_limit(pos["token_bought"])),
        check_eq(name, "unresolved_tokens", int(pos["unresolved_tokens"]), 0),
    ]


def moncock_checks(cur) -> list[Check]:
    name = "moncock"
    pos = position(cur, MONCOCK_WALLET, MONCOCK)
    if pos is None:
        return [check_missing(name, "position row")]
    realized = int(pos["realized_pnl_native"] or 0) + int(pos["realized_estimated_native"] or 0)
    return [
        check_abs(name, "token_bought", pos["token_bought"], "25719120.30", "0.01"),
        check_pct(name, "native_spent (confirmed + estimated)", pos["native_spent"], "477018", "0.5"),
        check_pct(name, "realized (confirmed + estimated)", realized, "-193957", "0.5"),
        Check(
            name,
            "realized split confirmed / estimated",
            "reported",
            f"{from_wei(pos['realized_pnl_native']):.3f} / {from_wei(pos['realized_estimated_native']):.3f}",
            True,
        ),
        check_le(name, "balance_token", int(pos["balance_token"]), dust_limit(pos["token_bought"])),
    ]


def rpc_call(rpc_url: str, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    if "result" not in payload:
        raise RuntimeError(str(payload)[:200])
    return payload["result"]


def chain_balance(rpc_url: str, token: str, wallet: str, block: int | None = None) -> int:
    data = BALANCE_OF_SELECTOR + wallet.lower().removeprefix("0x").rjust(64, "0")
    time.sleep(RPC_INTERVAL)
    tag = hex(int(block)) if block else "latest"
    raw = rpc_call(rpc_url, "eth_call", [{"to": token, "data": data}, tag])
    return int(raw, 16) if raw and raw != "0x" else 0


def chain_balances(rpc_url: str, token: str, wallets: list[str], block: int | None) -> tuple[dict[str, int], list[str]]:
    tag = hex(int(block)) if block else "latest"
    out: dict[str, int] = {}
    pending = list(wallets)
    for attempt in range(6):
        if not pending:
            break
        if attempt:
            time.sleep(3 * attempt)
        retry: list[str] = []
        for start in range(0, len(pending), CHAIN_BATCH):
            chunk = pending[start : start + CHAIN_BATCH]
            payload = [
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "eth_call",
                    "params": [{"to": token, "data": BALANCE_OF_SELECTOR + w.removeprefix("0x").rjust(64, "0")}, tag],
                }
                for i, w in enumerate(chunk)
            ]
            body = json.dumps(payload).encode()
            req = urllib.request.Request(rpc_url, data=body, headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    items = json.loads(resp.read())
            except Exception:
                items = []
            got = {it["id"]: it.get("result") for it in items if isinstance(it, dict) and "result" in it}
            for i, w in enumerate(chunk):
                raw = got.get(i)
                if raw is None:
                    retry.append(w)
                else:
                    out[w] = int(raw, 16) if raw and raw != "0x" else 0
            time.sleep(RPC_INTERVAL * len(chunk))
        pending = retry
    return out, pending


def wallet_kinds(cur, wallets: list[str]) -> dict[str, str]:
    if not wallets:
        return {}
    cur.execute("SELECT address, kind FROM address_kinds WHERE address = ANY(%s)", (wallets,))
    kinds = {a: k for a, k in cur.fetchall()}
    cur.execute("SELECT address FROM venues WHERE address = ANY(%s)", (wallets,))
    for (addr,) in cur.fetchall():
        kinds.setdefault(addr, "venue_pool")
    return kinds


def ledger_balances(cur, token: str) -> dict[str, int]:
    cur.execute("SELECT wallet, balance_token, custody_balance FROM positions_v2 WHERE token = %s", (token,))
    return {w: int(b or 0) + int(c or 0) for w, b, c in cur.fetchall()}


def prod_holders(cur, token: str) -> list[str]:
    cur.execute("SELECT user_address FROM launchpad_positions WHERE token = %s AND balance_token > 1", (token,))
    return sorted({(r[0] or "").lower() for r in cur.fetchall()})


def james_checks(cur, rpc_url: str | None, head: int | None = None) -> list[Check]:
    name = "JAMES"
    ledger = ledger_balances(cur, JAMES)
    prod = prod_holders(cur, JAMES)
    kinds = wallet_kinds(cur, sorted(set(ledger) | set(prod)))
    wallets = sorted(w for w in ledger if kinds.get(w) not in VENUE_KINDS)
    checks = [check_eq(name, "positions present", bool(wallets), True)]

    pools_on_prod = [w for w in prod if kinds.get(w) in VENUE_KINDS]
    holders = [w for w in prod if kinds.get(w) not in VENUE_KINDS]
    checks.append(
        Check(name, "prod holder rows that are venues", "reported", f"{len(pools_on_prod)} {pools_on_prod[:3]}", True)
    )
    missing = [w for w in holders if w not in ledger]
    if rpc_url is None:
        checks.append(
            Check(
                name,
                f"prod holders present ({len(holders)}, unverified against chain)",
                "0 missing",
                f"{len(missing)} missing {missing[:5]}",
                not missing,
            )
        )
        checks.append(Check(name, "chain balanceOf", "skipped", "skipped", True))
        return checks

    targets = sorted(set(wallets) | set(missing))
    balances, unreachable = chain_balances(rpc_url, JAMES, targets, head)
    where = f"at block {head:,}" if head else "at latest"
    held_at_head = [w for w in missing if balances.get(w, 0) > HOLDER_DUST_WEI]
    arrived_later = [w for w in missing if w in balances and balances[w] <= HOLDER_DUST_WEI]
    checks.append(
        Check(
            name,
            f"prod holders present ({len(holders)}, holding on chain {where})",
            "0 missing",
            f"{len(held_at_head)} missing {held_at_head[:5]}; {len(arrived_later)} of prod's holders bought after the head",
            not held_at_head,
        )
    )
    mismatched = [w for w in targets if w in balances and balances[w] != ledger.get(w, 0)]
    checks.append(
        Check(
            name,
            f"chain balanceOf == balance + custody ({len(targets)} wallets {where})",
            "0 mismatches",
            f"{len(mismatched)} mismatches {mismatched[:5]}",
            not mismatched,
        )
    )
    checks.append(
        Check(name, "chain reads that never answered", "0", f"{len(unreachable)} {unreachable[:3]}", not unreachable)
    )
    return checks


def invariant_checks(cur, tokens: list[str]) -> list[Check]:
    name = "invariants"
    cur.execute(
        """
        SELECT count(*) FROM positions_v2 p
        WHERE EXISTS (SELECT 1 FROM address_kinds k WHERE k.address = p.wallet AND k.kind = ANY(%s))
           OR EXISTS (SELECT 1 FROM venues v WHERE v.address = p.wallet)
        """,
        (sorted(VENUE_KINDS),),
    )
    venue_rows = int(cur.fetchone()[0])
    checks = [check_eq(name, "positions on venue addresses", venue_rows, 0)]
    for token, (est, unres, unpriced) in token_shares(cur, tokens).items():
        checks.append(Check(name, f"{token[:10]} estimated share", "reported", _pct(est), True))
        checks.append(Check(name, f"{token[:10]} inflow still without a cost", "reported", _pct(unres), True))
        checks.append(Check(name, f"{token[:10]} inflow with no price of its own", "reported", _pct(unpriced), True))
    return checks


def _pct(value) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.3f}%"


def selected_tokens(fixtures: list[str], tokens: list[str]) -> list[str]:
    unknown = [name for name in fixtures if name.lower() not in FIXTURES]
    if unknown:
        raise SystemExit(f"unknown fixture(s) {unknown}; choose from {sorted(FIXTURES)}")
    chosen = [FIXTURES[name.lower()] for name in fixtures] + [t.lower() for t in tokens]
    return list(dict.fromkeys(chosen)) or list(FIXTURE_TOKENS)


def run_checks(cur, rpc_url: str | None, tokens: list[str]) -> list[Check]:
    checks = []
    if CHIPOTLE in tokens:
        checks += chipotle_checks(cur)
    if MONCOCK in tokens:
        checks += moncock_checks(cur)
    if JAMES in tokens:
        cur.execute("SELECT MAX(block_number) FROM wallet_flows WHERE token = %s", (JAMES,))
        row = cur.fetchone()
        head = int(row[0]) if row and row[0] else None
        checks += james_checks(cur, rpc_url, head)
    checks += invariant_checks(cur, tokens)
    return checks


def main() -> None:
    ap = argparse.ArgumentParser(description="check the side database ledger against the fixtures")
    ap.add_argument("--rpc", default=os.environ.get("RPC_HTTP", "https://rpc.monad.xyz"))
    ap.add_argument("--skip-chain", action="store_true", help="skip the JAMES balanceOf comparison")
    ap.add_argument("--token", action="append", default=[], help="restrict to these fixture tokens")
    ap.add_argument("--fixture", action="append", default=[], help="restrict to these fixtures by name")
    args = ap.parse_args()

    import core.storage as storage

    storage.init_pool()
    tokens = selected_tokens(args.fixture, args.token)
    with storage.db_cursor() as cur:
        cur.execute("SELECT value FROM ledger_meta WHERE key = 'replay_head_block'")
        row = cur.fetchone()
        head = row[0] if row else "unknown"
        checks = run_checks(cur, None if args.skip_chain else args.rpc, tokens)
    print(f"ledger_meta replay_head_block: {head} (each token is compared at its own last folded block)")
    print(render_table(checks))
    passed = sum(c.ok for c in checks)
    print(f"{passed}/{len(checks)} checks passed")
    if not all_pass(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
