import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.lifecycle import (
    BPS_DENOMINATOR,
    GRADUATING_THRESHOLD_BPS,
    CurveState,
    TokenPhase,
    resolve_phase,
    snapshot,
)
from core.adapters.native import (
    CURVE_SUPPLY,
    GRADUATED_TOKEN_SUPPLY,
    INITIAL_TOKEN_SUPPLY,
    NativeLaunchpadAdapter,
)


def _curve(sold, supply=CURVE_SUPPLY):
    return CurveState(tokens_sold=sold, curve_supply=supply)


# --- lifecycle rules ----------------------------------------------------------

def test_phase_progression_created_active_graduating():
    assert resolve_phase(curve=None, has_trades=False) is TokenPhase.CREATED
    assert resolve_phase(curve=_curve(0), has_trades=True) is TokenPhase.ACTIVE
    assert resolve_phase(curve=_curve(CURVE_SUPPLY // 2), has_trades=True) is TokenPhase.ACTIVE
    assert resolve_phase(curve=_curve(CURVE_SUPPLY * 3 // 4), has_trades=True) is TokenPhase.GRADUATING


def test_terminal_states_win_over_curve_progress():
    """A curve can sit at 100% for a block before the graduation event lands, so
    graduation must come from an observed event, never inferred from progress."""
    mid = _curve(CURVE_SUPPLY // 2)
    assert resolve_phase(curve=mid, has_trades=True, graduated=True) is TokenPhase.GRADUATED
    assert resolve_phase(curve=mid, has_trades=True, migrated=True) is TokenPhase.MIGRATED
    # migrated outranks graduated
    assert (
        resolve_phase(curve=mid, has_trades=True, graduated=True, migrated=True)
        is TokenPhase.MIGRATED
    )
    # a full curve alone is still only "graduating"
    assert resolve_phase(curve=_curve(CURVE_SUPPLY), has_trades=True) is TokenPhase.GRADUATING


def test_progress_bps_math_and_clamping():
    assert _curve(0).progress_bps == 0
    assert _curve(CURVE_SUPPLY // 2).progress_bps == 5_000
    assert _curve(CURVE_SUPPLY * 3 // 4).progress_bps == GRADUATING_THRESHOLD_BPS
    assert _curve(CURVE_SUPPLY).progress_bps == BPS_DENOMINATOR
    # overshoot clamps instead of exceeding 100%
    assert _curve(CURVE_SUPPLY * 2).progress_bps == BPS_DENOMINATOR
    # degenerate supply does not divide by zero
    assert CurveState(tokens_sold=5, curve_supply=0).progress_bps == 0


def test_graduating_boundary_is_exact():
    threshold = CURVE_SUPPLY * GRADUATING_THRESHOLD_BPS // BPS_DENOMINATOR
    assert _curve(threshold - 1).is_graduating is False
    assert _curve(threshold).is_graduating is True


def test_snapshot_reports_full_progress_once_off_curve():
    snap = snapshot(curve=None, has_trades=True, graduated=True)
    assert snap.phase is TokenPhase.GRADUATED
    assert snap.progress_bps == BPS_DENOMINATOR
    assert snap.is_on_curve is False

    live = snapshot(curve=_curve(CURVE_SUPPLY // 4), has_trades=True)
    assert live.is_on_curve is True
    assert live.progress_ratio == Decimal("0.25")


# --- native adapter -----------------------------------------------------------

def test_native_geometry_matches_the_contract():
    assert INITIAL_TOKEN_SUPPLY == 10 ** 27
    assert GRADUATED_TOKEN_SUPPLY == 2 * 10 ** 26
    assert CURVE_SUPPLY == 8 * 10 ** 26
    assert CURVE_SUPPLY // 10 ** 18 == 800_000_000


def test_native_adapter_normalizes_reserves():
    a = NativeLaunchpadAdapter()
    v0 = 1000 * 10 ** 18
    k = v0 * INITIAL_TOKEN_SUPPLY

    # at creation: nothing sold
    st = a.curve_state({"native_reserve": v0, "token_reserve": INITIAL_TOKEN_SUPPLY})
    assert st.tokens_sold == 0
    assert st.progress_bps == 0

    # at the graduation reserve: fully sold
    st = a.curve_state({"native_reserve": 5 * v0, "token_reserve": GRADUATED_TOKEN_SUPPLY})
    assert st.tokens_sold == CURVE_SUPPLY
    assert st.is_complete is True
    assert st.progress_bps == BPS_DENOMINATOR

    # 75% of tokens sold
    tr = INITIAL_TOKEN_SUPPLY - (CURVE_SUPPLY * 3 // 4)
    st = a.curve_state({"native_reserve": k // tr, "token_reserve": tr})
    assert st.tokens_sold == 600_000_000 * 10 ** 18
    assert st.is_graduating is True


def test_native_adapter_rejects_unusable_events():
    a = NativeLaunchpadAdapter()
    assert a.curve_state({}) is None
    assert a.curve_state({"token_reserve": 0}) is None
    assert a.curve_state({"token_reserve": INITIAL_TOKEN_SUPPLY + 1}) is None
    assert a.curve_state({"token_reserve": "junk"}) is None


def test_native_adapter_initial_price_and_routing():
    a = NativeLaunchpadAdapter(lambda: 141_600 * 10 ** 18)
    assert a.initial_price_native() == Decimal(141_600 * 10 ** 18) / Decimal(INITIAL_TOKEN_SUPPLY)
    assert a.graduates_to_market() is True

    # unknown V0 must not fabricate a price
    assert NativeLaunchpadAdapter(lambda: 0).initial_price_native() is None
    assert NativeLaunchpadAdapter().initial_price_native() is None


def test_native_geometry_helpers_recover_v0_and_threshold():
    v0 = 49_300 * 10 ** 18
    k = v0 * INITIAL_TOKEN_SUPPLY
    assert NativeLaunchpadAdapter.initial_native_reserve(k) == v0
    assert NativeLaunchpadAdapter.graduation_native_reserve(k) == 5 * v0


# --- the model must not assume native geometry --------------------------------

def test_lifecycle_rules_hold_for_a_foreign_curve_geometry():
    """A source with a different supply split must get identical phase/progress
    semantics. If native constants ever leak into core.lifecycle, this fails."""
    foreign_curve_supply = 793_100_000 * 10 ** 18  # nad.fun-shaped, deliberately not ours
    assert foreign_curve_supply != CURVE_SUPPLY

    quarter = _curve(foreign_curve_supply // 4, supply=foreign_curve_supply)
    assert quarter.progress_bps == 2_500
    assert resolve_phase(curve=quarter, has_trades=True) is TokenPhase.ACTIVE

    three_quarters = _curve(foreign_curve_supply * 3 // 4, supply=foreign_curve_supply)
    assert three_quarters.progress_bps == GRADUATING_THRESHOLD_BPS
    assert resolve_phase(curve=three_quarters, has_trades=True) is TokenPhase.GRADUATING

    # and such a source can terminate in MIGRATED rather than GRADUATED
    assert (
        resolve_phase(curve=three_quarters, has_trades=True, migrated=True) is TokenPhase.MIGRATED
    )
