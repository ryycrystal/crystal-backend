from __future__ import annotations

from decimal import Decimal, getcontext
from typing import Any

getcontext().prec = 80

WMON_ADDR = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A".lower()
USDC_ADDR = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603".lower()
MON_USD_POOL = "0x659bD0BC4167BA25c62E05656F78043E7eD4a9da".lower()

LVMON_ADDR = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
LVMON_MON_POOL = "0xc59514136bdc9c0e735471cd650625ba0f5a634d"

WMON_DECIMALS = 18
USDC_DECIMALS = 6

_Q96 = Decimal(2) ** 96

_MON_USD_DECIMAL_SCALE = Decimal(10) ** (WMON_DECIMALS - USDC_DECIMALS)
_MIN_MON_WEI_FOR_SWAP_RATIO = 10**17
_MIN_PLAUSIBLE_MON_USD = Decimal("0.0001")
_MAX_PLAUSIBLE_MON_USD = Decimal("10000")


def _mon_usd_from_sqrt_price(sqrt_price_x96: int) -> Decimal | None:
    if sqrt_price_x96 <= 0:
        return None

    ratio = (Decimal(sqrt_price_x96) / _Q96) ** 2
    if ratio <= 0:
        return None

    return ratio * _MON_USD_DECIMAL_SCALE


def _mon_usd_from_swap_amounts(mon_amount: int, usd_amount: int) -> Decimal | None:
    if mon_amount == 0 or abs(mon_amount) < _MIN_MON_WEI_FOR_SWAP_RATIO:
        return None

    return Decimal(-usd_amount * 10**18) / Decimal(mon_amount * 10**6)


def mon_price_from_v3swap(ev: dict[str, Any]) -> Decimal | None:
    try:
        mon_amount = int(ev.get("amount0") or 0)
        usd_amount = int(ev.get("amount1") or 0)
        sqrt_price_x96 = int(ev.get("sqrt_price_x96") or 0)
    except (TypeError, ValueError):
        return None

    price = _mon_usd_from_sqrt_price(sqrt_price_x96)
    if price is None:
        price = _mon_usd_from_swap_amounts(mon_amount, usd_amount)

    if price is None or not (_MIN_PLAUSIBLE_MON_USD <= price <= _MAX_PLAUSIBLE_MON_USD):
        return None

    return price


def lvmon_rate_from_v3swap(ev: dict[str, Any]) -> Decimal | None:
    try:
        sqrt_price_x96 = int(ev.get("sqrt_price_x96") or 0)
    except (TypeError, ValueError):
        return None

    if sqrt_price_x96 <= 0:
        return None

    ratio = (Decimal(sqrt_price_x96) / _Q96) ** 2
    if ratio <= 0:
        return None

    rate = Decimal(1) / ratio
    if rate <= Decimal("0.5") or rate > Decimal("1.05"):
        return None
    return rate
