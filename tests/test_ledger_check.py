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
