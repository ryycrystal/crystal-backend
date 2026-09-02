from __future__ import annotations

from decimal import Decimal

from core.adapters.base import register
from core.lifecycle import CurveState

WAD = 10**18

SOURCE_V1 = 1
SOURCE_V2 = 2
SOURCES = (SOURCE_V1, SOURCE_V2)

V1_VIRTUAL_NATIVE_0 = 90_000 * WAD
V1_VIRTUAL_TOKEN_0 = 1_073_000_191 * WAD
V1_GRADUATION_VIRTUAL_TOKEN = 279_900_191 * WAD
V1_CURVE_SUPPLY = V1_VIRTUAL_TOKEN_0 - V1_GRADUATION_VIRTUAL_TOKEN

V2_VIRTUAL_NATIVE_0 = 70_000 * WAD
V2_VIRTUAL_TOKEN_0 = 1_060_569_000 * WAD
V2_GRADUATION_VIRTUAL_NATIVE = 295_000 * WAD
V2_K = V2_VIRTUAL_NATIVE_0 * V2_VIRTUAL_TOKEN_0
V2_GRADUATION_VIRTUAL_TOKEN = -(-V2_K // V2_GRADUATION_VIRTUAL_NATIVE)
V2_CURVE_SUPPLY = V2_VIRTUAL_TOKEN_0 - V2_GRADUATION_VIRTUAL_TOKEN

V1_FEE_RATE = Decimal("0.01")
V2_FEE_RATE = Decimal("0.02")

_GEOMETRY = {
    SOURCE_V1: {
        "virtual_native_0": V1_VIRTUAL_NATIVE_0,
        "virtual_token_0": V1_VIRTUAL_TOKEN_0,
        "curve_supply": V1_CURVE_SUPPLY,
        "fee_rate": V1_FEE_RATE,
    },
    SOURCE_V2: {
        "virtual_native_0": V2_VIRTUAL_NATIVE_0,
        "virtual_token_0": V2_VIRTUAL_TOKEN_0,
        "curve_supply": V2_CURVE_SUPPLY,
        "fee_rate": V2_FEE_RATE,
    },
}


def is_nadfun_source(source) -> bool:
    return int(source or 0) in SOURCES


def version_of(source) -> int:
    src = int(source or 0)
    return src if src in SOURCES else 0


def geometry_for(source) -> dict | None:
    return _GEOMETRY.get(int(source or 0))


def fee_rate_for(source) -> Decimal:
    geo = _GEOMETRY.get(int(source or 0))
    return geo["fee_rate"] if geo else Decimal(0)


def curve_supply_for(source) -> int:
    geo = _GEOMETRY.get(int(source or 0))
    return int(geo["curve_supply"]) if geo else 0


class NadfunLaunchpadAdapter:
    def __init__(self, source: int):
        geo = _GEOMETRY[int(source)]
        self.source = int(source)
        self.name = f"nadfun-v{source}"
        self.virtual_native_0 = int(geo["virtual_native_0"])
        self.virtual_token_0 = int(geo["virtual_token_0"])
        self.curve_supply = int(geo["curve_supply"])
        self.fee_rate = geo["fee_rate"]

    def curve_state(self, ev: dict) -> CurveState | None:
        if not ev:
            return None
        try:
            token_reserve = int(ev.get("token_reserve") or 0)
            native_reserve = int(ev.get("native_reserve") or 0)
        except (TypeError, ValueError):
            return None

        if token_reserve <= 0 or token_reserve > self.virtual_token_0 * 2:
            return None

        return CurveState(
            tokens_sold=max(self.virtual_token_0 - token_reserve, 0),
            curve_supply=self.curve_supply,
            native_reserve=native_reserve,
            token_reserve=token_reserve,
        )

    def initial_price_native(self) -> Decimal:
        return Decimal(self.virtual_native_0) / Decimal(self.virtual_token_0)

    def graduates_to_market(self) -> bool:
        return False


def build_all() -> dict[int, NadfunLaunchpadAdapter]:
    out = {}
    for src in SOURCES:
        adapter = NadfunLaunchpadAdapter(src)
        register(adapter)
        out[src] = adapter
    return out
