import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.adapters.native import (
    CURVE_SUPPLY,
    GRADUATED_TOKEN_SUPPLY,
    INITIAL_TOKEN_SUPPLY,
    NativeLaunchpadAdapter,
)
from core.lifecycle import (
    BPS_DENOMINATOR,
    GRADUATING_THRESHOLD_BPS,
    CurveState,
    TokenPhase,
    resolve_phase,
    snapshot,
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
    assert resolve_phase(curve=mid, has_trades=True, graduated=True, migrated=True) is TokenPhase.MIGRATED
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
    assert INITIAL_TOKEN_SUPPLY == 10**27
    assert GRADUATED_TOKEN_SUPPLY == 2 * 10**26
    assert CURVE_SUPPLY == 8 * 10**26
    assert CURVE_SUPPLY // 10**18 == 800_000_000


def test_native_adapter_normalizes_reserves():
    a = NativeLaunchpadAdapter()
    v0 = 1000 * 10**18
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
    assert st.tokens_sold == 600_000_000 * 10**18
    assert st.is_graduating is True


def test_native_adapter_rejects_unusable_events():
    a = NativeLaunchpadAdapter()
    assert a.curve_state({}) is None
    assert a.curve_state({"token_reserve": 0}) is None
    assert a.curve_state({"token_reserve": INITIAL_TOKEN_SUPPLY + 1}) is None
    assert a.curve_state({"token_reserve": "junk"}) is None


def test_native_adapter_initial_price_and_routing():
    a = NativeLaunchpadAdapter(lambda: 141_600 * 10**18)
    assert a.initial_price_native() == Decimal(141_600 * 10**18) / Decimal(INITIAL_TOKEN_SUPPLY)
    assert a.graduates_to_market() is True

    # unknown V0 must not fabricate a price
    assert NativeLaunchpadAdapter(lambda: 0).initial_price_native() is None
    assert NativeLaunchpadAdapter().initial_price_native() is None


def test_native_geometry_helpers_recover_v0_and_threshold():
    v0 = 49_300 * 10**18
    k = v0 * INITIAL_TOKEN_SUPPLY
    assert NativeLaunchpadAdapter.initial_native_reserve(k) == v0
    assert NativeLaunchpadAdapter.graduation_native_reserve(k) == 5 * v0


# --- the model must not assume native geometry --------------------------------


def test_api_lifecycle_fields_map_each_phase():
    """The API exposes phase derived from stored fields, so it cannot drift."""
    import api.api as api_mod

    def phase_of(**kw):
        return api_mod._lifecycle_fields(**kw)["phase"]

    # native, on the curve
    assert phase_of(source=0, circulating_supply=0, tx_count=0, migrated=False) == "created"
    assert phase_of(source=0, circulating_supply=100_000_000, tx_count=3, migrated=False) == "active"
    assert phase_of(source=0, circulating_supply=600_000_000, tx_count=9, migrated=False) == "graduating"
    # native terminal is graduated; nad.fun hands off to an external venue
    assert phase_of(source=0, circulating_supply=800_000_000, tx_count=9, migrated=True) == "graduated"
    assert phase_of(source=1, circulating_supply=793_100_000, tx_count=9, migrated=True) == "migrated"

    # progress uses each source's own curve supply
    assert (
        api_mod._lifecycle_fields(source=0, circulating_supply=600_000_000, tx_count=1, migrated=False)["progressBps"]
        == 7_500
    )
    assert (
        api_mod._lifecycle_fields(source=1, circulating_supply=594_825_000, tx_count=1, migrated=False)["progressBps"]
        == 7_500
    )


def test_lifecycle_rules_hold_for_a_foreign_curve_geometry():
    """A source with a different supply split must get identical phase/progress
    semantics. If native constants ever leak into core.lifecycle, this fails."""
    foreign_curve_supply = 793_100_000 * 10**18  # nad.fun-shaped, deliberately not ours
    assert foreign_curve_supply != CURVE_SUPPLY

    quarter = _curve(foreign_curve_supply // 4, supply=foreign_curve_supply)
    assert quarter.progress_bps == 2_500
    assert resolve_phase(curve=quarter, has_trades=True) is TokenPhase.ACTIVE

    three_quarters = _curve(foreign_curve_supply * 3 // 4, supply=foreign_curve_supply)
    assert three_quarters.progress_bps == GRADUATING_THRESHOLD_BPS
    assert resolve_phase(curve=three_quarters, has_trades=True) is TokenPhase.GRADUATING

    # and such a source can terminate in MIGRATED rather than GRADUATED
    assert resolve_phase(curve=three_quarters, has_trades=True, migrated=True) is TokenPhase.MIGRATED


def test_chart_prices_keep_sub_unit_movement():
    """A 0.1 MON buy against the 1000 MON test curve moves the scaled price by
    0.198. Truncating to an integer collapsed every candle to a flat 1000, which
    is what made small-trade charts look like a straight line."""
    from decimal import Decimal, getcontext

    import api.api  # noqa: F401
    from api.api import _scaled_price

    getcontext().prec = 60
    v0 = Decimal(1000 * 10**18)
    supply = Decimal(10**27)
    k = v0 * supply

    def price(native_reserve: Decimal) -> Decimal:
        token_reserve = (k + native_reserve - 1) // native_reserve
        return native_reserve / Decimal(token_reserve)

    at_creation = price(v0)
    after_buy = price(v0 + Decimal(10**17 * 99_000 // 100_000))

    assert _scaled_price(at_creation) == "1000"
    assert _scaled_price(after_buy) == "1000.198009801"
    assert _scaled_price(at_creation) != _scaled_price(after_buy), "candles must not flatten"
    assert _scaled_price(None) == "0"


def test_nadfun_geometry_matches_chain_verified_constants():
    """Recovered from CurveSync logs on graduated tokens. The k values reproduce
    exactly, which is what confirms them -- the published mainnet docs describe a
    later deployment (180,000 virtual MON) that is neither v1 nor v2."""
    from core.adapters import nadfun as nf

    assert nf.V1_VIRTUAL_NATIVE_0 == 90_000 * 10**18
    assert nf.V1_VIRTUAL_TOKEN_0 == 1_073_000_191 * 10**18
    assert nf.V1_CURVE_SUPPLY // 10**18 == 793_100_000

    assert nf.V2_VIRTUAL_NATIVE_0 == 70_000 * 10**18
    assert nf.V2_VIRTUAL_TOKEN_0 == 1_060_569_000 * 10**18
    assert nf.V2_GRADUATION_VIRTUAL_NATIVE == 295_000 * 10**18

    # v2 k must equal what the chain reported for TAD and BTS
    assert (nf.V2_VIRTUAL_NATIVE_0 // 10**18) * (nf.V2_VIRTUAL_TOKEN_0 // 10**18) == 74_239_830_000_000

    # observed graduation state on TAD: virt_tok 251,660,440.68
    assert abs(nf.V2_GRADUATION_VIRTUAL_TOKEN / 1e18 - 251_660_440.68) < 0.01
    # and the accumulated supply every graduated v2 token converged on
    assert abs(nf.V2_CURVE_SUPPLY / 1e18 - 808_908_559.32) < 0.01

    # the generations genuinely differ, which is the whole reason for two sources
    assert nf.V1_CURVE_SUPPLY != nf.V2_CURVE_SUPPLY


def test_each_generation_gets_its_own_progress_denominator():
    """v2 sold against v1's 793,100,000 reads ~2% high, which is what made
    approaching_75 fire at 73.5% instead of 75%."""
    import api.api as api_mod
    from core.adapters import nadfun as nf

    # v2's curve supply is not a whole number of tokens, so 75% of it rounds
    # down to just under the threshold -- one more whole token crosses it
    v2_three_quarters = -(-(nf.V2_CURVE_SUPPLY * 3 // 4) // 10**18)
    assert (
        api_mod._lifecycle_fields(
            source=nf.SOURCE_V2, circulating_supply=v2_three_quarters, tx_count=1, migrated=False
        )["progressBps"]
        == 7_500
    )

    v1_three_quarters = (nf.V1_CURVE_SUPPLY * 3 // 4) // 10**18
    assert (
        api_mod._lifecycle_fields(
            source=nf.SOURCE_V1, circulating_supply=v1_three_quarters, tx_count=1, migrated=False
        )["progressBps"]
        == 7_500
    )

    # the old shared denominator is what the bug looked like
    stale = api_mod._lifecycle_fields(
        source=nf.SOURCE_V1, circulating_supply=v2_three_quarters, tx_count=1, migrated=False
    )["progressBps"]
    assert stale > 7_500, "v2 supply against v1 geometry must read high"


def test_api_reports_source_1_for_every_nadfun_generation():
    """The frontend maps source === 1 onto "nadfun". Reporting 2 would silently
    reclassify every v2 token as crystal, with the wrong curve and trade path."""
    import api.api as api_mod
    from core.adapters import nadfun as nf

    assert api_mod._api_source(nf.SOURCE_V1) == 1
    assert api_mod._api_source(nf.SOURCE_V2) == 1
    assert api_mod._api_source(0) == 0

    # the generation still reaches the client, just via nadfunVersion
    assert api_mod._nadfun_version("0xabc", nf.SOURCE_V2) == 2
    assert api_mod._nadfun_version("0xabc", nf.SOURCE_V1) == 1
    assert api_mod._nadfun_version("0xabc", 0) == 0


def test_true_source_round_trips_through_the_wire_contract():
    """graduation pct is computed from serialized tokens, so it must recover the
    generation from nadfunVersion rather than the flattened source."""
    import api.api  # noqa: F401
    from api.routes.launchpad import _graduation_pct, _true_source
    from core.adapters import nadfun as nf

    assert _true_source({"source": 1, "nadfunVersion": 2}) == nf.SOURCE_V2
    assert _true_source({"source": 1, "nadfunVersion": 1}) == nf.SOURCE_V1
    assert _true_source({"source": 0, "nadfunVersion": 0}) == 0

    v2_full = nf.V2_CURVE_SUPPLY // 10**18
    pct = _graduation_pct(v2_full, _true_source({"source": 1, "nadfunVersion": 2}))
    assert abs(pct - 1.0) < 1e-6, f"a fully sold v2 curve must read 100%, got {pct}"


def test_nadfun_adapter_derives_supply_from_reserves():
    from core.adapters import nadfun as nf

    for src in nf.SOURCES:
        a = nf.NadfunLaunchpadAdapter(src)
        geo = nf.geometry_for(src)

        at_creation = a.curve_state(
            {"token_reserve": geo["virtual_token_0"], "native_reserve": geo["virtual_native_0"]}
        )
        assert at_creation.tokens_sold == 0
        assert at_creation.progress_bps == 0

        sold_all = geo["virtual_token_0"] - geo["curve_supply"]
        at_graduation = a.curve_state({"token_reserve": sold_all, "native_reserve": 1})
        assert at_graduation.progress_bps == 10_000
        assert at_graduation.is_complete is True

        # v2's curve supply is not a whole number of wei, so floor(75%) can land
        # a hair under -- check either side of the boundary rather than on it
        just_under = geo["virtual_token_0"] - (geo["curve_supply"] * 74 // 100)
        assert a.curve_state({"token_reserve": just_under, "native_reserve": 1}).is_graduating is False
        just_over = geo["virtual_token_0"] - (geo["curve_supply"] * 76 // 100)
        assert a.curve_state({"token_reserve": just_over, "native_reserve": 1}).is_graduating is True

        # nad.fun hands liquidity to an external venue
        assert a.graduates_to_market() is False


def test_nadfun_adapter_refuses_to_invent_a_curve_without_reserves():
    """Reserves ride on a separate CurveSync log. Treating a missing one as
    token_reserve=0 would report the whole curve sold."""
    from core.adapters import nadfun as nf

    a = nf.NadfunLaunchpadAdapter(nf.SOURCE_V2)
    assert a.curve_state({}) is None
    assert a.curve_state({"token_reserve": 0, "native_reserve": 0}) is None
    assert a.curve_state({"token_reserve": None}) is None
    assert a.curve_state({"token_reserve": "junk"}) is None
    # above the starting reserve is impossible on this curve
    assert a.curve_state({"token_reserve": nf.V2_VIRTUAL_TOKEN_0 + 1}) is None


def test_v1_adapter_reproduces_the_old_hardcoded_initial_price():
    """state.py carried Decimal("0.00008387696") for every nad.fun token. It was
    v1's 90,000/1,073,000,191 all along, and it is what broke change_pct."""
    from decimal import Decimal

    from core.adapters import nadfun as nf

    price = nf.NadfunLaunchpadAdapter(nf.SOURCE_V1).initial_price_native()
    assert abs(price - Decimal("0.00008387696")) < Decimal("1e-11")

    # v2 starts on a different curve, so it must not share that price
    assert nf.NadfunLaunchpadAdapter(nf.SOURCE_V2).initial_price_native() != price
