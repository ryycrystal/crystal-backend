from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.api import storage, ttl_cache
from api.routes.vaults import _cached_state

router = APIRouter()


# usd decimals for every quote asset that can hold referral rewards
def _quote_decimals() -> dict[str, int]:
    try:
        with storage.db_cursor() as cur:
            cur.execute("SELECT DISTINCT quote_address, quote_decimals FROM crystal_markets")
            return {str(r[0]).lower(): int(r[1] or 18) for r in cur.fetchall()}
    except Exception:
        return {}


# one call for the referrals page: who referred you, who you referred, what you earned
@router.get("/referral/{address}")
@ttl_cache("referral:summary", ttl_seconds=5)
def referral_summary(address: str) -> dict[str, Any]:
    addr = (address or "").lower()
    if not addr.startswith("0x") or len(addr) != 42:
        raise HTTPException(status_code=400, detail="invalid address")

    binding = storage.get_referral_binding(addr)
    referrer = None
    referred_at = None
    if binding and str(binding[0] or "") not in ("", "0x" + "0" * 40):
        referrer = str(binding[0]).lower()
        referred_at = int(binding[2] or 0)

    referee_rows = storage.list_referees(addr)
    referees = [{"address": str(r[0]).lower(), "since": int(r[1] or 0)} for r in referee_rows]

    reward_rows = storage.get_referral_rewards(addr)
    dec_map = _quote_decimals()
    try:
        st = _cached_state()
    except Exception:
        st = None

    rewards = []
    total_claimable_usd = 0.0
    total_earned_usd = 0.0
    for r in reward_rows:
        tok = str(r[0]).lower()
        claimable = int(r[1] or 0)
        earned = int(r[2] or 0)
        dec = dec_map.get(tok, 18)
        price = float(st.tokenToPrice.get(tok, 0) or 0) if st is not None else 0.0
        claimable_usd = (claimable / (10**dec)) * price
        earned_usd = (earned / (10**dec)) * price
        total_claimable_usd += claimable_usd
        total_earned_usd += earned_usd
        rewards.append(
            {
                "token": tok,
                "claimable": str(claimable),
                "earned": str(earned),
                "claimableUsd": claimable_usd,
                "earnedUsd": earned_usd,
                "updatedAt": int(r[3] or 0),
            }
        )

    return {
        "ok": True,
        "address": addr,
        "referrer": referrer,
        "referredAt": referred_at,
        "referredCount": len(referees),
        "referees": referees,
        "rewards": rewards,
        "totalClaimableUsd": total_claimable_usd,
        "totalEarnedUsd": total_earned_usd,
    }
