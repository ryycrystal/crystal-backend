from __future__ import annotations

import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import core.rewards as rewards
from api.api import storage
from core.storage.base import db_cursor

router = APIRouter()

PREFIX = "/" + os.getenv("REWARDS_PATH_PREFIX", "results").strip("/")
ADMIN_KEY = os.getenv("REWARDS_ADMIN_KEY", "")
ZERO_ADDR = "0x" + "0" * 40

VOLUME_COLS = (
    ("pregrad_usd", "pregradUsd"),
    ("grad_usd", "gradUsd"),
    ("spot_taker_usd", "spotTakerUsd"),
    ("spot_maker_usd", "spotMakerUsd"),
    ("stable_taker_usd", "stableTakerUsd"),
    ("stable_maker_usd", "stableMakerUsd"),
    ("vault_usd_hours", "vaultUsdHours"),
)


def _require_admin(req: Request) -> None:
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="rewards admin key not configured")
    if not hmac.compare_digest(req.headers.get("x-admin-key", ""), ADMIN_KEY):
        raise HTTPException(status_code=403, detail="forbidden")


def _require_address(address: str) -> str:
    addr = (address or "").lower()
    if not addr.startswith("0x") or len(addr) != 42 or addr == ZERO_ADDR:
        raise HTTPException(status_code=400, detail="invalid address")
    try:
        int(addr, 16)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid address") from None
    return addr


@router.get(PREFIX + "/status")
def rewards_status() -> dict[str, Any]:
    now = int(time.time())
    start = rewards.program_start_ts()
    ws = rewards.bucket_for(max(now, start), start)
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*), COALESCE(SUM(crystals), 0) FROM crystal_rewards_balances")
        n_wallets, total_crystals = cur.fetchone()
        cur.execute(
            "SELECT week_start, week_end, participants, finalized FROM crystal_rewards_weeks ORDER BY week_start DESC LIMIT 8"
        )
        weeks = [
            {"weekStart": int(a), "weekEnd": int(b), "participants": int(c), "finalized": bool(d)}
            for a, b, c, d in cur.fetchall()
        ]
    return {
        "ok": True,
        "now": now,
        "programStart": start,
        "vaultStart": rewards.vault_start_ts(),
        "predepositCutoff": rewards.predeposit_cutoff_ts(),
        "predepositMultiplier": rewards.predeposit_multiplier(),
        "currentWeekStart": ws,
        "currentWeekEnd": rewards.bucket_end(ws),
        "rates": rewards.rates(),
        "pool": rewards.pool_size(),
        "exponent": rewards.exponent(),
        "milestones": rewards.milestones(),
        "watermarks": {
            "launchpad": storage.get_meta("rewards_wm_launchpad"),
            "spotTaker": storage.get_meta("rewards_wm_spot_taker"),
            "spotMaker": storage.get_meta("rewards_wm_spot_maker"),
            "vaultHour": storage.get_meta("rewards_wm_vault_hour"),
        },
        "wallets": int(n_wallets or 0),
        "totalCrystals": float(total_crystals or 0),
        "weeks": weeks,
    }


@router.get(PREFIX + "/week/{week_start}")
def rewards_week(week_start: int, limit: int = 100) -> dict[str, Any]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT week_end, pool, exponent, participants, total_raw, total_adjusted, finalized, finalized_at "
            "FROM crystal_rewards_weeks WHERE week_start = %s",
            (int(week_start),),
        )
        wk = cur.fetchone()
        cur.execute(
            """
            SELECT wallet, raw_points, adjusted, share, crystals, rank, status
            FROM crystal_rewards_distributions
            WHERE week_start = %s ORDER BY rank, wallet LIMIT %s
            """,
            (int(week_start), max(1, min(int(limit), 1000))),
        )
        dist = [
            {
                "wallet": w,
                "rawPoints": float(rp),
                "adjusted": float(adj),
                "share": float(sh),
                "crystals": float(c),
                "rank": int(rk),
                "status": st,
            }
            for w, rp, adj, sh, c, rk, st in cur.fetchall()
        ]
    return {
        "ok": True,
        "weekStart": int(week_start),
        "week": None
        if not wk
        else {
            "weekEnd": int(wk[0]),
            "pool": float(wk[1]),
            "exponent": float(wk[2]),
            "participants": int(wk[3]),
            "totalRaw": float(wk[4]),
            "totalAdjusted": float(wk[5]),
            "finalized": bool(wk[6]),
            "finalizedAt": int(wk[7] or 0),
        },
        "distributions": dist,
    }


@router.get(PREFIX + "/volumes/{address}")
def rewards_volumes(address: str) -> dict[str, Any]:
    addr = _require_address(address)
    cols = ", ".join(c for c, _ in VOLUME_COLS)
    sums = ", ".join(f"COALESCE(SUM({c}), 0)" for c, _ in VOLUME_COLS)
    with db_cursor() as cur:
        cur.execute(
            f"SELECT week_start, {cols}, points FROM crystal_rewards_contrib "
            "WHERE wallet = %s ORDER BY week_start DESC LIMIT 60",
            (addr,),
        )
        weekly = [
            {
                "weekStart": int(row[0]),
                **{name: float(row[i + 1]) for i, (_c, name) in enumerate(VOLUME_COLS)},
                "points": float(row[-1]),
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            f"SELECT {sums}, COALESCE(SUM(points), 0) FROM crystal_rewards_contrib WHERE wallet = %s",
            (addr,),
        )
        row = cur.fetchone()
        lifetime = {name: float(row[i]) for i, (_c, name) in enumerate(VOLUME_COLS)}
        lifetime["points"] = float(row[-1])
    return {"ok": True, "address": addr, "lifetime": lifetime, "weekly": weekly}


@router.get(PREFIX + "/wallet/{address}")
def rewards_wallet(address: str) -> dict[str, Any]:
    addr = _require_address(address)
    now = int(time.time())
    start = rewards.program_start_ts()
    ws = rewards.bucket_for(max(now, start), start)
    cols = ", ".join(c for c, _ in VOLUME_COLS)
    with db_cursor() as cur:
        cur.execute("SELECT crystals, updated_at FROM crystal_rewards_balances WHERE wallet = %s", (addr,))
        bal = cur.fetchone()
        cur.execute(
            f"SELECT week_start, {cols}, bonus_points, points FROM crystal_rewards_contrib "
            "WHERE wallet = %s ORDER BY week_start DESC LIMIT 16",
            (addr,),
        )
        contrib = [
            {
                "weekStart": int(row[0]),
                **{name: float(row[i + 1]) for i, (_c, name) in enumerate(VOLUME_COLS)},
                "bonusPoints": float(row[-2]),
                "points": float(row[-1]),
                "current": int(row[0]) == ws,
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT week_start, crystals, rank, participants, status
            FROM crystal_rewards_distributions WHERE wallet = %s ORDER BY week_start DESC LIMIT 16
            """,
            (addr,),
        )
        dist = [
            {"weekStart": int(a), "crystals": float(b), "rank": int(c), "participants": int(d), "status": e}
            for a, b, c, d, e in cur.fetchall()
        ]
        cur.execute(
            "SELECT ts, kind, amount, ref FROM crystal_rewards_grants WHERE wallet = %s ORDER BY ts DESC LIMIT 32",
            (addr,),
        )
        grants = [{"ts": int(a), "kind": b, "amount": float(c), "ref": d} for a, b, c, d in cur.fetchall()]
    return {
        "ok": True,
        "address": addr,
        "crystals": float(bal[0]) if bal else 0.0,
        "updatedAt": int(bal[1]) if bal else 0,
        "contrib": contrib,
        "distributions": dist,
        "grants": grants,
        "badges": sorted({d["status"] for d in dist}) if dist else [],
    }


@router.post(PREFIX + "/run")
def rewards_run(req: Request) -> dict[str, Any]:
    _require_admin(req)
    holder = f"manual-{rewards.NODE_ID}"
    if not storage.claim_rewards_leader(holder, rewards.LEADER_TTL):
        raise HTTPException(status_code=409, detail="rewards worker holds leadership; try again later")
    return {"ok": True, "result": rewards.run_once(leader_holder=holder)}
