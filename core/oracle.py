from __future__ import annotations
from decimal import Decimal, getcontext
from typing import Optional, Dict, Any

getcontext().prec = 80

WMON_ADDR = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A".lower()
USDC_ADDR = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603".lower()
MON_USD_POOL = "0x659bD0BC4167BA25c62E05656F78043E7eD4a9da".lower()

WMON_DECIMALS = 18
USDC_DECIMALS = 6

def mon_price_from_v3swap(ev: Dict[str, Any]) -> Optional[Decimal]:
    try:
        mon_amount = int(ev.get("amount0") or 0)
        usd_amount = int(ev.get("amount1") or 0)
    except (TypeError, ValueError):
        return None

    if mon_amount == 0:
        return None

    return Decimal(-usd_amount * 10 ** 18) / Decimal(mon_amount * 10 ** 6)