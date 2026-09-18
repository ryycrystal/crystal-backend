from __future__ import annotations

from decimal import Decimal

from core.adapters.base import register
from core.lifecycle import CurveState

INITIAL_TOKEN_SUPPLY = 10**27
GRADUATED_TOKEN_SUPPLY = 2 * 10**26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY

VIRTUAL_TOKEN_SUPPLY = (
    GRADUATED_TOKEN_SUPPLY * GRADUATED_TOKEN_SUPPLY + (INITIAL_TOKEN_SUPPLY - 2 * GRADUATED_TOKEN_SUPPLY) - 1
) // (INITIAL_TOKEN_SUPPLY - 2 * GRADUATED_TOKEN_SUPPLY)
VIRTUAL_NATIVE_SUPPLY = 100_000 * 10**18

INITIAL_CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY + VIRTUAL_TOKEN_SUPPLY
GRADUATED_CURVE_SUPPLY = GRADUATED_TOKEN_SUPPLY + VIRTUAL_TOKEN_SUPPLY


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

        if token_reserve <= 0 or token_reserve > INITIAL_CURVE_SUPPLY * 2:
            return None

        return CurveState(
            tokens_sold=max(INITIAL_CURVE_SUPPLY - token_reserve, 0),
            curve_supply=CURVE_SUPPLY,
            native_reserve=native_reserve,
            token_reserve=token_reserve,
        )

    def initial_price_native(self) -> Decimal:
        v0 = 0
        fn = self._initial_native_supply_fn
        if fn is not None:
            try:
                v0 = int(fn() or 0)
            except Exception:
                v0 = 0
        if v0 <= 0:
            v0 = VIRTUAL_NATIVE_SUPPLY
        return Decimal(v0) / Decimal(INITIAL_CURVE_SUPPLY)

    def graduates_to_market(self) -> bool:
        return True

    @staticmethod
    def graduation_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        return k // GRADUATED_CURVE_SUPPLY

    @staticmethod
    def initial_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        return k // INITIAL_CURVE_SUPPLY


def build(initial_native_supply_fn=None) -> NativeLaunchpadAdapter:
    adapter = NativeLaunchpadAdapter(initial_native_supply_fn)
    register(adapter)
    return adapter
