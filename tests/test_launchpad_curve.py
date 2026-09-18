import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.adapters import native  # noqa: E402

SUPPLY = native.INITIAL_TOKEN_SUPPLY
G = native.GRADUATED_TOKEN_SUPPLY
V = native.VIRTUAL_TOKEN_SUPPLY
IC = native.INITIAL_CURVE_SUPPLY
GC = native.GRADUATED_CURVE_SUPPLY
N0 = 3 * 10**20


def adapter():
    return native.NativeLaunchpadAdapter(lambda: N0)


def test_virtual_supply_matches_the_contract_formula():
    assert V == 66_666_666_666_666_666_666_666_667
    assert IC == SUPPLY + V
    assert GC == G + V


def test_curve_supply_is_800m():
    assert IC - GC == native.CURVE_SUPPLY == 8 * 10**26


def test_curve_state_accepts_the_virtual_reserve_range():
    a = adapter()
    st = a.curve_state({"token_reserve": IC, "native_reserve": N0})
    assert st is not None and st.tokens_sold == 0
    st = a.curve_state({"token_reserve": IC - 5 * 10**25, "native_reserve": N0})
    assert st.tokens_sold == 5 * 10**25
    assert a.curve_state({"token_reserve": IC + 1, "native_reserve": N0}).tokens_sold == 0
    assert a.curve_state({"token_reserve": IC * 3, "native_reserve": N0}) is None


def test_graduation_progress_reaches_exactly_100_pct():
    a = adapter()
    st = a.curve_state({"token_reserve": GC, "native_reserve": 4 * N0})
    assert st.tokens_sold == native.CURVE_SUPPLY
    assert st.progress_bps == 10000
    assert st.is_graduating


def test_graduation_target_is_4x_initial_native():
    k = N0 * IC
    target = native.NativeLaunchpadAdapter.graduation_native_reserve(k)
    assert abs(target - 4 * N0) <= 1
    assert native.NativeLaunchpadAdapter.initial_native_reserve(k) == N0


def test_initial_price_uses_the_virtual_supply():
    assert adapter().initial_price_native() == Decimal(N0) / Decimal(IC)


def test_curve_state_survives_a_one_wei_reserve_overshoot():
    state = adapter().curve_state({"token_reserve": IC + 1, "native_reserve": N0})
    assert state is not None
    assert state.token_reserve == IC + 1
    assert state.tokens_sold == 0


def test_curve_state_still_rejects_an_absurd_reserve():
    assert adapter().curve_state({"token_reserve": IC * 3, "native_reserve": N0}) is None
    assert adapter().curve_state({"token_reserve": 0, "native_reserve": N0}) is None


def test_virtual_native_supply_is_100k_mon():
    assert native.VIRTUAL_NATIVE_SUPPLY == 100_000 * 10**18


def test_launch_price_uses_virtual_supply_on_both_sides_when_the_fetch_reads_zero():
    expected = Decimal(native.VIRTUAL_NATIVE_SUPPLY) / Decimal(IC)
    assert native.NativeLaunchpadAdapter(lambda: 0).initial_price_native() == expected
    assert native.NativeLaunchpadAdapter().initial_price_native() == expected
    assert (
        native.NativeLaunchpadAdapter(lambda: (_ for _ in ()).throw(RuntimeError())).initial_price_native() == expected
    )


def test_launch_price_is_never_the_model_placeholder():
    price = native.NativeLaunchpadAdapter(lambda: 0).initial_price_native()
    assert price > Decimal("0.000001") * 50


def test_the_retired_9_16_core_launched_at_200k_backed_out_of_real_first_buys():
    price = Decimal(200_000 * 10**18) / Decimal(IC)
    for spent_mon, got_tokens, post_price in (
        (8_000, 40_631_012, Decimal("0.000202644030000000")),
        (223, 1_176_142, Decimal("0.000187914172215904")),
    ):
        net_in = Decimal(spent_mon * 10**18) * Decimal("0.99")
        token_after = Decimal(IC) - Decimal(got_tokens * 10**18)
        native_before = post_price * token_after - net_in
        assert abs(native_before / Decimal(IC) - price) / price < Decimal("0.0001")


def test_a_real_fetched_supply_wins_over_the_constant():
    fetched = 250_000 * 10**18
    assert native.NativeLaunchpadAdapter(lambda: fetched).initial_price_native() == Decimal(fetched) / Decimal(IC)
