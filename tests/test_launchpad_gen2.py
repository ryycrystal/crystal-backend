import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.adapters import native  # noqa: E402

SUPPLY = native.INITIAL_TOKEN_SUPPLY
G = native.GRADUATED_TOKEN_SUPPLY
V = native.VIRTUAL_TOKEN_SUPPLY
IC, GC = native.GEN_SUPPLIES[2]
N0 = 3 * 10**20


def adapter():
    return native.NativeLaunchpadAdapter(lambda: N0)


def test_virtual_supply_matches_the_contract_formula():
    assert V == 66_666_666_666_666_666_666_666_667
    assert IC == SUPPLY + V
    assert GC == G + V


def test_curve_supply_is_800m_in_both_generations():
    assert IC - GC == native.CURVE_SUPPLY == 8 * 10**26


def test_gen_defaults_to_1(monkeypatch):
    monkeypatch.delenv("CRYSTAL_LAUNCHPAD_GEN", raising=False)
    assert native.launchpad_generation() == 1
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "junk")
    assert native.launchpad_generation() == 1
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "7")
    assert native.launchpad_generation() == 1
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    assert native.launchpad_generation() == 2


def test_gen1_math_is_unchanged(monkeypatch):
    monkeypatch.delenv("CRYSTAL_LAUNCHPAD_GEN", raising=False)
    a = adapter()
    st = a.curve_state({"token_reserve": SUPPLY - 10**24, "native_reserve": N0})
    assert st.tokens_sold == 10**24
    assert a.curve_state({"token_reserve": SUPPLY + 1, "native_reserve": N0}).tokens_sold == 0
    assert a.curve_state({"token_reserve": SUPPLY * 3, "native_reserve": N0}) is None
    assert a.initial_price_native() == Decimal(N0) / Decimal(SUPPLY)
    k = N0 * SUPPLY
    assert native.NativeLaunchpadAdapter.graduation_native_reserve(k) == k // G


def test_gen2_accepts_the_virtual_reserve_range(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    a = adapter()
    st = a.curve_state({"token_reserve": IC, "native_reserve": N0})
    assert st is not None and st.tokens_sold == 0
    st = a.curve_state({"token_reserve": IC - 5 * 10**25, "native_reserve": N0})
    assert st.tokens_sold == 5 * 10**25
    assert a.curve_state({"token_reserve": IC + 1, "native_reserve": N0}).tokens_sold == 0
    assert a.curve_state({"token_reserve": IC * 3, "native_reserve": N0}) is None


def test_gen2_graduation_progress_reaches_exactly_100_pct(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    a = adapter()
    st = a.curve_state({"token_reserve": GC, "native_reserve": 4 * N0})
    assert st.tokens_sold == native.CURVE_SUPPLY
    assert st.progress_bps == 10000
    assert st.is_graduating


def test_gen2_graduation_target_is_4x_initial_native(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    k = N0 * IC
    target = native.NativeLaunchpadAdapter.graduation_native_reserve(k)
    assert abs(target - 4 * N0) <= 1
    assert native.NativeLaunchpadAdapter.initial_native_reserve(k) == N0


def test_gen2_initial_price_uses_the_virtual_supply(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    assert adapter().initial_price_native() == Decimal(N0) / Decimal(IC)


def test_curve_state_survives_a_one_wei_reserve_overshoot(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    state = adapter().curve_state({"token_reserve": IC + 1, "native_reserve": N0})
    assert state is not None
    assert state.token_reserve == IC + 1
    assert state.tokens_sold == 0


def test_curve_state_still_rejects_an_absurd_reserve(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    assert adapter().curve_state({"token_reserve": IC * 3, "native_reserve": N0}) is None
    assert adapter().curve_state({"token_reserve": 0, "native_reserve": N0}) is None


def test_gen2_virtual_native_supply_is_200k_mon():
    assert native.VIRTUAL_NATIVE_SUPPLY == 200_000 * 10**18
    assert native.GEN_VIRTUAL_NATIVE[2] == native.VIRTUAL_NATIVE_SUPPLY
    assert native.GEN_VIRTUAL_NATIVE[1] == 0


def test_gen2_launch_price_uses_virtual_supply_on_both_sides_when_the_fetch_reads_zero(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    expected = Decimal(native.VIRTUAL_NATIVE_SUPPLY) / Decimal(IC)
    assert native.NativeLaunchpadAdapter(lambda: 0).initial_price_native() == expected
    assert native.NativeLaunchpadAdapter().initial_price_native() == expected
    assert (
        native.NativeLaunchpadAdapter(lambda: (_ for _ in ()).throw(RuntimeError())).initial_price_native() == expected
    )


def test_gen2_launch_price_is_never_the_model_placeholder(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    price = native.NativeLaunchpadAdapter(lambda: 0).initial_price_native()
    assert price is not None and price > Decimal("0.000001") * 100


def test_gen2_launch_price_matches_reserves_backed_out_of_real_first_buys(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    price = native.NativeLaunchpadAdapter(lambda: 0).initial_price_native()
    for spent_mon, got_tokens, post_price in (
        (8_000, 40_631_012, Decimal("0.000202644030000000")),
        (223, 1_176_142, Decimal("0.000187914172215904")),
    ):
        net_in = Decimal(spent_mon * 10**18) * Decimal("0.99")
        token_after = Decimal(IC) - Decimal(got_tokens * 10**18)
        native_before = post_price * token_after - net_in
        assert abs(native_before / Decimal(IC) - price) / price < Decimal("0.0001")


def test_gen2_prefers_a_real_fetched_supply_over_the_constant(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    fetched = 250_000 * 10**18
    assert native.NativeLaunchpadAdapter(lambda: fetched).initial_price_native() == Decimal(fetched) / Decimal(IC)


def test_gen1_still_returns_none_when_the_fetch_reads_zero(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "1")
    assert native.NativeLaunchpadAdapter(lambda: 0).initial_price_native() is None
    assert native.NativeLaunchpadAdapter().initial_price_native() is None
