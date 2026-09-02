from __future__ import annotations

import os
from decimal import Decimal

from core.adapters.base import register
from core.lifecycle import CurveState

INITIAL_TOKEN_SUPPLY = 10**27
GRADUATED_TOKEN_SUPPLY = 2 * 10**26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY

VIRTUAL_TOKEN_SUPPLY = (
    GRADUATED_TOKEN_SUPPLY * GRADUATED_TOKEN_SUPPLY + (INITIAL_TOKEN_SUPPLY - 2 * GRADUATED_TOKEN_SUPPLY) - 1
) // (INITIAL_TOKEN_SUPPLY - 2 * GRADUATED_TOKEN_SUPPLY)

GEN_SUPPLIES = {
    1: (INITIAL_TOKEN_SUPPLY, GRADUATED_TOKEN_SUPPLY),
    2: (INITIAL_TOKEN_SUPPLY + VIRTUAL_TOKEN_SUPPLY, GRADUATED_TOKEN_SUPPLY + VIRTUAL_TOKEN_SUPPLY),
}


def launchpad_generation() -> int:
    try:
        gen = int(os.getenv("CRYSTAL_LAUNCHPAD_GEN", "1"))
    except ValueError:
        return 1
    return gen if gen in GEN_SUPPLIES else 1


class NativeLaunchpadAdapter:
    source = 0
    name = "crystal-native"

    def __init__(self, initial_native_supply_fn=None):
        self._initial_native_supply_fn = initial_native_supply_fn

    @staticmethod
    def _supplies() -> tuple[int, int]:
        return GEN_SUPPLIES[launchpad_generation()]

    def curve_state(self, ev: dict) -> CurveState | None:
        if not ev:
            return None
        try:
            token_reserve = int(ev.get("token_reserve") or 0)
            native_reserve = int(ev.get("native_reserve") or 0)
        except (TypeError, ValueError):
            return None

        initial_curve_supply, _ = self._supplies()
        if token_reserve <= 0 or token_reserve > initial_curve_supply * 2:
            return None

        return CurveState(
            tokens_sold=max(initial_curve_supply - token_reserve, 0),
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
        initial_curve_supply, _ = self._supplies()
        return Decimal(v0) / Decimal(initial_curve_supply)

    def graduates_to_market(self) -> bool:
        return True

    @staticmethod
    def graduation_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        _, graduated_curve_supply = NativeLaunchpadAdapter._supplies()
        return k // graduated_curve_supply

    @staticmethod
    def initial_native_reserve(k: int) -> int:
        if k <= 0:
            return 0
        initial_curve_supply, _ = NativeLaunchpadAdapter._supplies()
        return k // initial_curve_supply


def build(initial_native_supply_fn=None) -> NativeLaunchpadAdapter:
    adapter = NativeLaunchpadAdapter(initial_native_supply_fn)
    register(adapter)
    return adapter
