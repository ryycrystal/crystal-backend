# nad.fun launchpad geometry, verified on chain rather than from docs
#
# each generation is a different curve and gets its own internal source id, the
# published api still reports source 1 for both with nadfunVersion carrying the
# generation, because the frontend maps source === 1 onto "nadfun"
#
# constants were recovered from CurveSync logs on graduated tokens, the k values
# reproduce exactly which is what confirms them
#
#   v1  0xA7283d...  virt mon 90,000   virt token 1,073,000,191
#                    k = 96,570,017,190,000
#                    graduates when virt token falls to 279,900,191
#                    curve supply 793,100,000
#
#   v2  0x9f3832...  virt mon 70,000   virt token 1,060,569,000
#                    k = 74,239,830,000,000
#                    graduates when virt mon reaches 295,000
#                    curve supply 808,908,559.32
#
# the docs published for monad mainnet describe a later deployment (180,000 virt
# mon) that is neither of these, do not use them for v1 or v2

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
# the curve ceils the token reserve the same way crystal's does, flooring here
# left tokens_sold one wei short of the supply so a completed v2 curve read 9999
V2_GRADUATION_VIRTUAL_TOKEN = -(-V2_K // V2_GRADUATION_VIRTUAL_NATIVE)
V2_CURVE_SUPPLY = V2_VIRTUAL_TOKEN_0 - V2_GRADUATION_VIRTUAL_TOKEN

# fee taken on each trade, measured from real trades rather than assumed:
#   v1  REDNIT   reserve delta 0.98950500 -> user got 0.97960995  = 1%
#   v2  BTS      amount in 4,000 -> reserve delta 3,920           = 2%
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


# true for any nad.fun generation
def is_nadfun_source(source) -> bool:
    return int(source or 0) in SOURCES


# generation number for a source id, 0 when the source is not nad.fun
def version_of(source) -> int:
    src = int(source or 0)
    return src if src in SOURCES else 0


# curve geometry for a source id, none when the source is not nad.fun
def geometry_for(source) -> dict | None:
    return _GEOMETRY.get(int(source or 0))


# fraction taken as fee on each trade, for a source id
def fee_rate_for(source) -> Decimal:
    geo = _GEOMETRY.get(int(source or 0))
    return geo["fee_rate"] if geo else Decimal(0)


# tokens the curve sells before graduating, for a source id
def curve_supply_for(source) -> int:
    geo = _GEOMETRY.get(int(source or 0))
    return int(geo["curve_supply"]) if geo else 0


# maps one nad.fun generation onto the normalized lifecycle model
class NadfunLaunchpadAdapter:
    # each generation is a separate curve so it registers under its own source
    def __init__(self, source: int):
        geo = _GEOMETRY[int(source)]
        self.source = int(source)
        self.name = f"nadfun-v{source}"
        self.virtual_native_0 = int(geo["virtual_native_0"])
        self.virtual_token_0 = int(geo["virtual_token_0"])
        self.curve_supply = int(geo["curve_supply"])
        self.fee_rate = geo["fee_rate"]

    # normalize a trade into curve state using the reserves the CurveSync carried
    def curve_state(self, ev: dict) -> CurveState | None:
        # reserves arrive from a separate CurveSync log, so they are absent when
        # that log was missed, reordered or lost across a restart, returning None
        # leaves supply untouched rather than reporting a fully sold curve
        if not ev:
            return None
        try:
            token_reserve = int(ev.get("token_reserve") or 0)
            native_reserve = int(ev.get("native_reserve") or 0)
        except (TypeError, ValueError):
            return None

        if token_reserve <= 0 or token_reserve > self.virtual_token_0:
            return None

        return CurveState(
            tokens_sold=self.virtual_token_0 - token_reserve,
            curve_supply=self.curve_supply,
            native_reserve=native_reserve,
            token_reserve=token_reserve,
        )

    # spot price of a freshly created token from the starting virtual reserves
    def initial_price_native(self) -> Decimal:
        return Decimal(self.virtual_native_0) / Decimal(self.virtual_token_0)

    # nad.fun hands liquidity to an external venue, so it terminates at migrated
    def graduates_to_market(self) -> bool:
        return False


# construct both generation adapters and register them by source
def build_all() -> dict[int, NadfunLaunchpadAdapter]:
    out = {}
    for src in SOURCES:
        adapter = NadfunLaunchpadAdapter(src)
        register(adapter)
        out[src] = adapter
    return out
