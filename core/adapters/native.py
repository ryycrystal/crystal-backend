"""Native Crystal launchpad adapter.

Geometry (contracts/core/Crystal.sol):

    INITIAL_TOKEN_SUPPLY    = 1e27   (1,000,000,000 tokens, 18dp) minted to the core
    GRADUATED_TOKEN_SUPPLY  = 2e26   (200,000,000 tokens) reserved for the AMM
    => curve supply         = 8e26   (800,000,000 tokens, 80% of supply)

The curve starts at ``virtualNativeReserve = V0`` (``launchpadInitialNativeSupply``,
governance-settable) and ``virtualTokenReserve = INITIAL_TOKEN_SUPPLY``, with
``k = V0 * INITIAL_TOKEN_SUPPLY`` held constant. Graduation fires when
``virtualNativeReserve >= k / GRADUATED_TOKEN_SUPPLY`` (== 5 * V0), i.e. exactly
when the token reserve has fallen to GRADUATED_TOKEN_SUPPLY.

Only the native side is virtual: the core contract really holds all 1e27 tokens,
so token reserve and real balance coincide while on the curve.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from core.adapters.base import register
from core.lifecycle import CurveState

INITIAL_TOKEN_SUPPLY = 10 ** 27
GRADUATED_TOKEN_SUPPLY = 2 * 10 ** 26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY


class NativeLaunchpadAdapter:
    source = 0
    name = "crystal-native"

    def __init__(self, initial_native_supply_fn=None):
        # injected so the adapter stays testable and does no I/O at import time
        self._initial_native_supply_fn = initial_native_supply_fn

    # -- lifecycle ---------------------------------------------------------

    def curve_state(self, ev: dict) -> Optional[CurveState]:
        """Map a LaunchpadTrade payload onto normalized curve state.

        ``token_reserve`` is authoritative on-chain state, so tokens_sold is
        derived from it rather than accumulated across trades -- a running sum
        desyncs permanently on a single missed event.
        """
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

    def initial_price_native(self) -> Optional[Decimal]:
        """V0 / INITIAL_TOKEN_SUPPLY -- the curve's spot price before any trade.

        V0 is governance-settable, so it is read rather than assumed.
        """
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

    def graduates_to_market(self) -> bool:
        # native tokens graduate onto a Crystal market and stop there
        return True

    # -- geometry helpers --------------------------------------------------

    @staticmethod
    def graduation_native_reserve(k: int) -> int:
        """Native reserve at which the curve completes (== 5 * V0)."""
        if k <= 0:
            return 0
        return k // GRADUATED_TOKEN_SUPPLY

    @staticmethod
    def initial_native_reserve(k: int) -> int:
        """V0 recovered from the curve invariant."""
        if k <= 0:
            return 0
        return k // INITIAL_TOKEN_SUPPLY


def build(initial_native_supply_fn=None) -> NativeLaunchpadAdapter:
    adapter = NativeLaunchpadAdapter(initial_native_supply_fn)
    register(adapter)
    return adapter
