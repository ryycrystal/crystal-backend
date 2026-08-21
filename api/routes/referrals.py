from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.api import storage, ttl_cache

router = APIRouter()


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
    rewards = [
        {
            "token": str(r[0]).lower(),
            "claimable": str(int(r[1] or 0)),
            "earned": str(int(r[2] or 0)),
            "updatedAt": int(r[3] or 0),
        }
        for r in reward_rows
    ]

    return {
        "ok": True,
        "address": addr,
        "referrer": referrer,
        "referredAt": referred_at,
        "referredCount": len(referees),
        "referees": referees,
        "rewards": rewards,
    }
