# native crystal launchpad adapter
#
# geometry from contracts/core/Crystal.sol
#   initial token supply    = 1e27  (1,000,000,000 tokens, 18dp) minted to the core
#   graduated token supply  = 2e26  (200,000,000 tokens) reserved for the amm
#   curve supply            = 8e26  (800,000,000 tokens, 80% of supply)
#
# the curve starts at virtualNativeReserve = v0 (launchpadInitialNativeSupply,
# governance settable) and virtualTokenReserve = initial token supply, with
# k = v0 * initial token supply held constant, graduation fires when
# virtualNativeReserve >= k / graduated token supply, equal to 5 x v0, exactly
# when the token reserve has fallen to the graduated token supply
#
# only the native side is virtual, the core really holds all 1e27 tokens so token
# reserve and real balance coincide while on the curve

from __future__ import annotations

from decimal import Decimal

from core.adapters.base import register
from core.lifecycle import CurveState

INITIAL_TOKEN_SUPPLY = 10**27
GRADUATED_TOKEN_SUPPLY = 2 * 10**26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY


# maps crystal native launchpad events onto the normalized lifecycle model
class NativeLaunchpadAdapter:
    source = 0
    name = "crystal-native"

    # takes v0 as an injected callable so the adapter does no io at import time
    def __init__(self, initial_native_supply_fn=None):
        self._initial_native_supply_fn = initial_native_supply_fn

    # -- lifecycle ---------------------------------------------------------

    # normalize a launchpadtrade payload into curve state, tokens sold comes from
    # the reported reserve because a running total desyncs on one missed event
    def curve_state(self, ev: dict) -> CurveState | None:
        if not ev:
            return None
        try:
            token_reserve = int(ev.get("token_reserve") or 0)
            native_reserve = int(ev.get("native_reserve") or 0)
        except (TypeError, ValueError):
            return None

        if token_reserve <= 0 or token_reserve > INITIAL_TOKEN_SUPPLY:
            return None

        return CurveState(
            tokens_sold=INITIAL_TOKEN_SUPPLY - token_reserve,
            curve_supply=CURVE_SUPPLY,
            native_reserve=native_reserve,
            token_reserve=token_reserve,
        )

    # spot price of a freshly created token, v0 over initial supply
    def initial_price_native(self) -> Decimal | None:
        fn = self._initial_native_supply_fn
        if fn is None:
            return None
        try:
            v0 = int(fn() or 0)
        except Exception:
            return None
        if v0 <= 0:
            return None
        return Decimal(v0) / Decimal(INITIAL_TOKEN_SUPPLY)

    # native tokens graduate onto a crystal market and stop there
    def graduates_to_market(self) -> bool:
        return True

    # -- geometry helpers --------------------------------------------------

    # native reserve at which the curve completes, equal to 5 x v0
    @staticmethod
    def graduation_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        return k // GRADUATED_TOKEN_SUPPLY

    # v0 recovered from the curve invariant
    @staticmethod
    def initial_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        return k // INITIAL_TOKEN_SUPPLY


# construct the adapter and register it in the source registry
def build(initial_native_supply_fn=None) -> NativeLaunchpadAdapter:
    adapter = NativeLaunchpadAdapter(initial_native_supply_fn)
    register(adapter)
    return adapter
