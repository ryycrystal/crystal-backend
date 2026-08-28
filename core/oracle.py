from __future__ import annotations

from decimal import Decimal, getcontext
from typing import Any

getcontext().prec = 80

WMON_ADDR = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A".lower()
USDC_ADDR = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603".lower()
MON_USD_POOL = "0x659bD0BC4167BA25c62E05656F78043E7eD4a9da".lower()

# lvmon is leverup's synthetic mon: minted 1:1 against mon collateral but redeemed
# only against a daily quota at the vault's own ratio, so it is a claim on that
# vault rather than a wrapper and it trades below parity. pricing it as mon would
# overstate every token quoted in it, so the rate comes off its own pool
LVMON_ADDR = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
LVMON_MON_POOL = "0xc59514136bdc9c0e735471cd650625ba0f5a634d"

WMON_DECIMALS = 18
USDC_DECIMALS = 6

# both sides of the lvmon pool are 18 decimals, so the sqrt price needs no scaling
_Q96 = Decimal(2) ** 96


# derive mon usd from a wmon usdc v3 swap, scaling for 18 vs 6 decimals
def mon_price_from_v3swap(ev: dict[str, Any]) -> Decimal | None:
    try:
        mon_amount = int(ev.get("amount0") or 0)
        usd_amount = int(ev.get("amount1") or 0)
    except (TypeError, ValueError):
        return None

    if mon_amount == 0:
        return None

    return Decimal(-usd_amount * 10**18) / Decimal(mon_amount * 10**6)


# lvmon priced in mon from its v3 pool. token0 is wmon and token1 is lvmon, so the
# pool price is lvmon per wmon and the rate we want is its reciprocal. the post swap
# sqrt price is the pool's own price, unlike the swap amounts which carry the fee
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
    # a wrapper that has broken this far from parity is a depeg, not a quote, and
    # the caller should keep its last good rate rather than price tokens off it
    if rate <= Decimal("0.5") or rate > Decimal("1.05"):
        return None
    return rate
