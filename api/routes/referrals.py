from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException

from api.api import storage, ttl_cache

router = APIRouter()

ZERO_ADDR = "0x" + "0" * 40
MAX_REFEREES = 500


def _require_address(address: str) -> str:
    addr = (address or "").lower()
    if not addr.startswith("0x") or len(addr) != 42 or addr == ZERO_ADDR:
        raise HTTPException(status_code=400, detail="invalid address")
    try:
        int(addr, 16)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid address") from None
    return addr


def _quote_decimals() -> dict[str, int]:
    try:
        with storage.db_cursor() as cur:
            cur.execute("SELECT DISTINCT quote_address, quote_decimals FROM crystal_markets")
            return {str(r[0]).lower(): (int(r[1]) if r[1] is not None else 18) for r in cur.fetchall()}
    except Exception:
        return {}


def _price_state():
    from api.routes.vaults import _cached_state

    return _cached_state()


@router.get("/referral/{address}")
@ttl_cache("referral:summary", ttl_seconds=5)
def referral_summary(address: str) -> dict[str, Any]:
    addr = _require_address(address)

    binding = storage.get_referral_binding(addr)
    referrer = None
    referred_at = None
    if binding and str(binding[0] or "") not in ("", ZERO_ADDR):
        referrer = str(binding[0]).lower()
        referred_at = int(binding[2] or 0)

    referee_rows = storage.list_referees(addr, limit=MAX_REFEREES)
    referees = [{"address": str(r[0]).lower(), "since": int(r[1] or 0)} for r in referee_rows]

    reward_rows = storage.get_referral_rewards(addr)
    dec_map = _quote_decimals() if reward_rows else {}
    st = None
    if reward_rows:
        try:
            st = _price_state()
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


def _tier_row(r) -> dict[str, Any]:
    return {
        "tier": int(r[0]),
        "name": str(r[1] or ""),
        "minVolumeUsd": float(r[2] or 0),
        "cashbackMultiplier": float(r[3] or 0),
        "referralCommissionBps": int(r[4] or 0),
        "referralCommissionPercent": int(r[4] or 0) / 100.0,
    }


def _resolve_tier(volume_usd: float, tiers: list[dict]) -> tuple[dict | None, dict | None]:
    current = None
    nxt = None
    for t in tiers:
        if volume_usd >= t["minVolumeUsd"]:
            current = t
        elif nxt is None:
            nxt = t
    return current, nxt


@router.get("/tiers")
@ttl_cache("tiers:ladder", ttl_seconds=60)
def volume_tier_ladder() -> dict[str, Any]:
    tiers = [_tier_row(r) for r in storage.list_volume_tiers()]
    return {"ok": True, "windowDays": storage.tier_window_days(), "tiers": tiers}


@router.get("/tiers/{address}")
@ttl_cache("tiers:wallet", ttl_seconds=15)
def volume_tier_for_wallet(address: str) -> dict[str, Any]:
    addr = _require_address(address)

    window_days = storage.tier_window_days()
    since_ts = int(time.time()) - (window_days * 86400) if window_days > 0 else 0

    volume_raw, trade_count = storage.wallet_launchpad_volume_usd(addr, since_ts)
    volume_usd = float(volume_raw or 0)

    tiers = [_tier_row(r) for r in storage.list_volume_tiers()]
    current, nxt = _resolve_tier(volume_usd, tiers)

    remaining = max(nxt["minVolumeUsd"] - volume_usd, 0.0) if nxt else 0.0
    progress_bps = 10000
    if nxt:
        floor_usd = current["minVolumeUsd"] if current else 0.0
        span = nxt["minVolumeUsd"] - floor_usd
        progress_bps = int(max(min((volume_usd - floor_usd) / span, 1.0), 0.0) * 10000) if span > 0 else 0

    return {
        "ok": True,
        "address": addr,
        "windowDays": window_days,
        "volumeUsd": volume_usd,
        "tradeCount": trade_count,
        "tier": current,
        "nextTier": nxt,
        "remainingUsd": remaining,
        "progressBps": progress_bps,
        "tiers": tiers,
    }
