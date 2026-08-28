from __future__ import annotations

from decimal import Decimal

from core.adapters.base import register
from core.lifecycle import CurveState

INITIAL_TOKEN_SUPPLY = 10**27
GRADUATED_TOKEN_SUPPLY = 2 * 10**26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY


class NativeLaunchpadAdapter:
    source = 0
    name = "crystal-native"

    def __init__(self, initial_native_supply_fn=None):
        self._initial_native_supply_fn = initial_native_supply_fn

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

    def graduates_to_market(self) -> bool:
        return True

    @staticmethod
    def graduation_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        return k // GRADUATED_TOKEN_SUPPLY

    @staticmethod
    def initial_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        return k // INITIAL_TOKEN_SUPPLY


def build(initial_native_supply_fn=None) -> NativeLaunchpadAdapter:
    adapter = NativeLaunchpadAdapter(initial_native_supply_fn)
    register(adapter)
    return adapter
