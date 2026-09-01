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

ADMIN_KEY = os.getenv("REWARDS_ADMIN_KEY", "")
ZERO_ADDR = "0x" + "0" * 40


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


@router.get("/rewards/status")
def rewards_status(req: Request) -> dict[str, Any]:
    _require_admin(req)
    now = int(time.time())
    start = rewards.program_start_ts()
    ws = rewards.week_start_for(max(now, start))
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
        "currentWeekStart": ws,
        "currentWeekEnd": rewards.week_end_for(ws),
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


@router.get("/rewards/week/{week_start}")
def rewards_week(week_start: int, req: Request, limit: int = 100) -> dict[str, Any]:
    _require_admin(req)
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
                "wallet": w, "rawPoints": float(rp), "adjusted": float(adj),
                "share": float(sh), "crystals": float(c), "rank": int(rk), "status": st,
            }
            for w, rp, adj, sh, c, rk, st in cur.fetchall()
        ]
    return {
        "ok": True,
        "weekStart": int(week_start),
        "week": None if not wk else {
            "weekEnd": int(wk[0]), "pool": float(wk[1]), "exponent": float(wk[2]),
            "participants": int(wk[3]), "totalRaw": float(wk[4]), "totalAdjusted": float(wk[5]),
            "finalized": bool(wk[6]), "finalizedAt": int(wk[7] or 0),
        },
        "distributions": dist,
    }


@router.get("/rewards/{address}")
def rewards_wallet(address: str, req: Request) -> dict[str, Any]:
    _require_admin(req)
    addr = _require_address(address)
    now = int(time.time())
    ws = rewards.week_start_for(max(now, rewards.program_start_ts()))
    with db_cursor() as cur:
        cur.execute("SELECT crystals, updated_at FROM crystal_rewards_balances WHERE wallet = %s", (addr,))
        bal = cur.fetchone()
        cur.execute(
            """
            SELECT week_start, pregrad_usd, grad_usd, spot_taker_usd, spot_maker_usd,
                   stable_taker_usd, stable_maker_usd, vault_usd_hours, bonus_points, points
            FROM crystal_rewards_contrib WHERE wallet = %s ORDER BY week_start DESC LIMIT 16
            """,
            (addr,),
        )
        contrib = [
            {
                "weekStart": int(r[0]), "pregradUsd": float(r[1]), "gradUsd": float(r[2]),
                "spotTakerUsd": float(r[3]), "spotMakerUsd": float(r[4]),
                "stableTakerUsd": float(r[5]), "stableMakerUsd": float(r[6]),
                "vaultUsdHours": float(r[7]), "bonusPoints": float(r[8]), "points": float(r[9]),
                "current": int(r[0]) == ws,
            }
            for r in cur.fetchall()
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


@router.post("/rewards/run")
def rewards_run(req: Request) -> dict[str, Any]:
    _require_admin(req)
    return {"ok": True, "result": rewards.run_once()}
