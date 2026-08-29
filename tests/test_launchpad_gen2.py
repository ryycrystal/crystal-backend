import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.adapters import native  # noqa: E402

I = native.INITIAL_TOKEN_SUPPLY
G = native.GRADUATED_TOKEN_SUPPLY
V = native.VIRTUAL_TOKEN_SUPPLY
IC, GC = native.GEN_SUPPLIES[2]
N0 = 3 * 10**20


def adapter():
    return native.NativeLaunchpadAdapter(lambda: N0)


def test_virtual_supply_matches_the_contract_formula():
    assert V == 66_666_666_666_666_666_666_666_667
    assert IC == I + V
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
    st = a.curve_state({"token_reserve": I - 10**24, "native_reserve": N0})
    assert st.tokens_sold == 10**24
    assert a.curve_state({"token_reserve": I + 1, "native_reserve": N0}) is None
    assert a.initial_price_native() == Decimal(N0) / Decimal(I)
    k = N0 * I
    assert native.NativeLaunchpadAdapter.graduation_native_reserve(k) == k // G


def test_gen2_accepts_the_virtual_reserve_range(monkeypatch):
    monkeypatch.setenv("CRYSTAL_LAUNCHPAD_GEN", "2")
    a = adapter()
    st = a.curve_state({"token_reserve": IC, "native_reserve": N0})
    assert st is not None and st.tokens_sold == 0
    st = a.curve_state({"token_reserve": IC - 5 * 10**25, "native_reserve": N0})
    assert st.tokens_sold == 5 * 10**25
    assert a.curve_state({"token_reserve": IC + 1, "native_reserve": N0}) is None


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
