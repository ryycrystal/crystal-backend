from decimal import Decimal

import pytest

from scripts.ledger_check import (
    CHIPOTLE,
    DUST_WEI,
    FIXTURE_TOKENS,
    JAMES,
    MONCOCK,
    Check,
    all_pass,
    check_abs,
    check_eq,
    check_le,
    check_pct,
    dust_limit,
    estimated_share,
    from_wei,
    render_table,
    selected_tokens,
    unresolved_share,
    within_abs,
    within_pct,
)

WEI = 10**18


def test_from_wei_is_exact_decimal():
    assert from_wei(13850173 * 10**15) == Decimal("13850.173")
    assert from_wei(None) == Decimal(0)
    assert from_wei(Decimal("1")) == Decimal(1) / Decimal(WEI)


def test_within_abs_boundaries():
    assert within_abs("20724.081", "20724.081", "0.01")
    assert within_abs(Decimal("20724.09"), "20724.081", "0.01")
    assert not within_abs(Decimal("20724.0911"), "20724.081", "0.01")
    assert within_abs("-5", "-5.005", "0.01")


def test_within_pct_handles_sign_and_zero():
    assert within_pct(Decimal("-193957"), "-193957", "0.5")
    assert within_pct(Decimal("-194900"), "-193957", "0.5")
    assert not within_pct(Decimal("-195000"), "-193957", "0.5")
    assert within_pct(0, 0, "0.5")
    assert not within_pct(1, 0, "0.5")


def test_check_abs_reports_units_from_wei():
    ok = check_abs("f", "native_spent", 20724081 * 10**15, "20724.081", "0.01")
    assert ok.ok and ok.actual.startswith("20724.081")
    bad = check_abs("f", "native_spent", 20725 * WEI, "20724.081", "0.01")
    assert not bad.ok


def test_check_pct_and_eq_and_le():
    assert check_pct("f", "cost", 477018 * WEI, "477018", "0.5").ok
    assert check_pct("f", "cost", 480000 * WEI, "477018", "0.5").ok is False
    assert check_eq("f", "trade_count", 20, 20).ok
    assert not check_eq("f", "trade_count", 15, 20).ok
    assert check_le("f", "balance", DUST_WEI, DUST_WEI).ok
    assert not check_le("f", "balance", DUST_WEI + 1, DUST_WEI).ok


def test_render_table_marks_pass_and_fail():
    checks = [
        Check("CHIPOTLE", "trade_count", "20", "20", True),
        Check("moncock", "balance_token", "<= 1000000000000000", "5", True),
        Check("JAMES", "chain balanceOf", "0 mismatches", "2 mismatches", False),
    ]
    table = render_table(checks)
    lines = table.splitlines()
    assert lines[0].startswith("fixture")
    assert lines[1].startswith("-")
    assert sum("PASS" in line for line in lines) == 2
    assert sum("FAIL" in line for line in lines) == 1
    assert not all_pass(checks)
    assert all_pass(checks[:2])


def test_estimated_share_is_estimated_over_total_basis():
    rows = [
        {"cost_basis_native": 300, "basis_estimated_native": 100},
        {"cost_basis_native": 100, "basis_estimated_native": 0},
    ]
    assert estimated_share(rows) == Decimal(100) / Decimal(500)
    assert estimated_share([]) is None
    assert estimated_share([{"cost_basis_native": 0, "basis_estimated_native": 0}]) is None


def test_unresolved_share_counts_held_custody_and_sold():
    rows = [
        {"balance_token": 10, "custody_balance": 5, "token_sold": 25, "unresolved_tokens": 10},
        {"balance_token": 0, "custody_balance": 0, "token_sold": 50, "unresolved_tokens": 0},
    ]
    assert unresolved_share(rows) == Decimal(10) / Decimal(100)
    assert unresolved_share([{"balance_token": 0, "token_sold": 0, "unresolved_tokens": 0}]) is None


def test_selected_tokens_resolves_fixture_names_case_insensitively():
    assert selected_tokens(["CHIPOTLE"], []) == [CHIPOTLE]
    assert selected_tokens(["moncock", "James"], []) == [MONCOCK, JAMES]
    assert selected_tokens([], []) == list(FIXTURE_TOKENS)
    assert selected_tokens(["chipotle"], [CHIPOTLE.upper()]) == [CHIPOTLE]
    with pytest.raises(SystemExit):
        selected_tokens(["nope"], [])


def test_dust_limit_is_one_billionth_of_the_bought_amount_but_never_below_dust_wei():
    assert dust_limit(0) == DUST_WEI
    assert dust_limit(None) == DUST_WEI
    assert dust_limit(10**9 * WEI) == WEI
    assert dust_limit(3_173_915_918 * WEI) == 3_173_915_918 * 10**9
    assert check_le("f", "balance_token", 1_147_748_982_595_449, dust_limit(3_173_915_918 * WEI)).ok
    assert not check_le("f", "balance_token", 1_147_748_982_595_449, dust_limit(0)).ok


class _PositionCursor:
    def __init__(self, row: tuple | None):
        self._row = row

    def execute(self, sql, params=None):
        assert "FROM positions_v2" in sql

    def fetchone(self):
        return self._row


def _moncock_row(realized_confirmed: int, realized_estimated: int) -> tuple:
    from scripts.ledger_check import MONCOCK_WALLET, POSITION_COLUMNS

    values = {
        "wallet": MONCOCK_WALLET,
        "token": MONCOCK,
        "balance_token": 5,
        "custody_balance": 0,
        "token_bought": 25_719_120_300_077_260_030_074_648,
        "token_sold": 25_719_120_300_077_260_030_074_648,
        "native_spent": 479_108 * WEI,
        "native_received": 282_154 * WEI,
        "cost_basis_native": 0,
        "realized_pnl_native": realized_confirmed,
        "basis_estimated_native": 0,
        "realized_estimated_native": realized_estimated,
        "unresolved_tokens": 0,
        "unresolved_proceeds_native": 0,
        "trade_count": 5,
        "buy_count": 4,
        "sell_count": 1,
    }
    return tuple(values[column] for column in POSITION_COLUMNS)


def test_moncock_realized_counts_the_estimated_legs_and_reports_the_split():
    from scripts.ledger_check import moncock_checks

    checks = moncock_checks(_PositionCursor(_moncock_row(-40_084 * WEI, -156_870 * WEI)))
    by_name = {c.name: c for c in checks}
    assert all_pass(checks), render_table(checks)
    assert by_name["realized (confirmed + estimated)"].actual.startswith("-196954")
    assert by_name["realized split confirmed / estimated"].actual == "-40084.000 / -156870.000"

    checks = moncock_checks(_PositionCursor(_moncock_row(-40_084 * WEI, 0)))
    assert not by_name_ok(checks, "realized (confirmed + estimated)")
    assert moncock_checks(_PositionCursor(None))[0].actual == "missing"


def by_name_ok(checks, name: str) -> bool:
    return next(c for c in checks if c.name == name).ok


class _ScriptedCursor:
    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self._rows: list = []

    def execute(self, sql, params=None):
        for needle, rows in self.answers.items():
            if needle in sql:
                self._rows = list(rows)
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_a_contract_holding_more_on_chain_than_the_ledger_books_is_reported_and_a_person_fails(monkeypatch):
    """A person's wallet has to match chain to the wei. An unclassified contract that differs is reported for
    review rather than failing the run: other venues' pools and fee sinks are contracts too, and the product
    owner reviews the ones that hold anything."""
    import scripts.ledger_check as lc

    bot, missing_bot = "0x" + "b0" * 20, "0x" + "b1" * 20
    cur = _ScriptedCursor(
        {
            "FROM positions_v2": [(bot, 0, 0)],
            "FROM launchpad_positions": [(missing_bot,)],
            "FROM address_kinds": [(bot, "contract_unknown"), (missing_bot, "contract_unknown")],
            "FROM venues": [],
        }
    )
    monkeypatch.setattr(
        lc, "chain_balances", lambda rpc, token, wallets, block: ({w: 1_000_000 * WEI for w in wallets}, [])
    )
    checks = lc.james_checks(cur, "http://rpc", 100)
    by_name = {c.name.split(" (")[0]: c for c in checks}
    assert by_name["chain balanceOf == balance + custody"].ok, render_table(checks)
    assert by_name["unclassified contracts whose chain balance differs from the ledger"].actual.startswith("2 ")
    assert not by_name["prod holders present"].ok, render_table(checks)

    person = "0x" + "11" * 20
    cur = _ScriptedCursor(
        {
            "FROM positions_v2": [(person, 5 * WEI, 0)],
            "FROM launchpad_positions": [(person,)],
            "FROM address_kinds": [(person, "eoa")],
            "FROM venues": [],
        }
    )
    monkeypatch.setattr(lc, "chain_balances", lambda rpc, token, wallets, block: ({person: 6 * WEI}, []))
    checks = lc.james_checks(cur, "http://rpc", 100)
    by_name = {c.name.split(" (")[0]: c for c in checks}
    assert not by_name["chain balanceOf == balance + custody"].ok, render_table(checks)


def test_chain_reads_that_never_answered_do_not_let_the_balance_check_pass(monkeypatch):
    import scripts.ledger_check as lc

    holder = "0x" + "11" * 20
    cur = _ScriptedCursor(
        {
            "FROM positions_v2": [(holder, 5 * WEI, 0)],
            "FROM launchpad_positions": [(holder,)],
            "FROM address_kinds": [(holder, "eoa")],
            "FROM venues": [],
        }
    )
    monkeypatch.setattr(lc, "chain_balances", lambda rpc, token, wallets, block: ({}, list(wallets)))
    checks = lc.james_checks(cur, "http://rpc", 100)
    by_name = {c.name.split(" (")[0]: c for c in checks}
    assert not by_name["chain balanceOf == balance + custody"].ok, render_table(checks)
