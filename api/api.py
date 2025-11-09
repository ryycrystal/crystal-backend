from __future__ import annotations
from typing import Dict, Any, List, Deque, Tuple
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import time

from core.sequencer import SEQUENCER
from state import INTERVALS, LABEL
from api.x_api import router as x_router
import models

_TIMEFRAME_SEC = {
    1: 86400,
    2: 7 * 86400,
    3: 30 * 86400,
    4: 10 * 365 * 86400,
}

def _pick_effective_step(age_sec: int, requested_window: int) -> int:
    window = min(max(0, age_sec), max(0, requested_window))
    if window >= 30 * 86400 and age_sec >= 30 * 86400:
        return 86400
    if window >= 7 * 86400:
        return 21600
    if window >= 86400:
        return 3600
    return 900

def _row_to_dict(b: Any) -> Dict[str, Any]:
    if isinstance(b, dict):
        return {
            "block": int(b.get("block", 0)),
            "timestamp": int(b.get("timestamp", 0)),
            "quoteBalance": int(b.get("quoteBalance", 0)),
            "baseBalance": int(b.get("baseBalance", 0)),
            "usdValue": float(b.get("usdValue", 0.0)),
        }
    return {
        "block": int(getattr(b, "block", 0)),
        "timestamp": int(getattr(b, "timestamp", 0)),
        "quoteBalance": int(getattr(b, "quoteBalance", 0)),
        "baseBalance": int(getattr(b, "baseBalance", 0)),
        "usdValue": float(getattr(b, "usdValue", 0.0)),
    }

def _pick_bucket(vaddr: str) -> Dict[int, Deque[models.VaultBalance]]:
    st = SEQUENCER._state
    return {
        900: st.vaultBalancesDay.get(vaddr, None),
        3600: st.vaultBalancesWeek.get(vaddr, None),
        21600: st.vaultBalancesMonth.get(vaddr, None),
        86400: st.vaultBalancesAllTime.get(vaddr, None),
    }

def _latest_balance_for(vaddr: str) -> Dict[str, Any]:
    dq = SEQUENCER._state.vaultBalancesAllTime.get(vaddr)
    if not dq:
        return {"quoteBalance": 0, "baseBalance": 0, "timestamp": 0, "usdValue": 0.0}
    row = _row_to_dict(dq[-1])
    return {
        "quoteBalance": int(row["quoteBalance"]),
        "baseBalance": int(row["baseBalance"]),
        "timestamp": int(row["timestamp"]),
        "usdValue": float(row["usdValue"]),
    }

def _build_history(v: models.Vault, timeframe: int, limit: int) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    if timeframe not in _TIMEFRAME_SEC:
        raise HTTPException(status_code=400, detail="invalid timeframe")

    now = int(time.time())
    created_ts = int(getattr(v, "timestamp", 0)) or 0
    age = max(0, now - created_ts)

    requested_window = _TIMEFRAME_SEC[timeframe]
    step = _pick_effective_step(age, requested_window)

    buckets = _pick_bucket(v.vault.lower())
    dq = buckets.get(step)

    if (not dq) or len(dq) == 0:
        for fallback in [3600, 21600, 900, 86400]:
            if fallback == step:
                continue
            cand = buckets.get(fallback)
            if cand and len(cand) > 0:
                dq = cand
                step = fallback
                break

    if not dq or len(dq) == 0:
        return (
            {"step": step, "count": 0, "effectiveWindowSec": 0, "requestedWindowSec": requested_window, "ageSec": age},
            {"tvl": [], "pnl": []},
        )

    pts_raw = list(dq)[-limit:] if limit and limit > 0 else list(dq)
    pts = [_row_to_dict(b) for b in pts_raw]

    tvl_series = [{"timestamp": p["timestamp"], "tvlUsd": p["usdValue"]} for p in pts]

    base_usd = pts[0]["usdValue"] if pts else 0.0
    pnl_series: List[Dict[str, Any]] = []
    for p in pts:
        cur = p["usdValue"]
        pnl_usd = cur - base_usd
        pnl_pct = ((cur / base_usd) - 1.0) * 100.0 if base_usd > 0 else 0.0
        pnl_series.append({"timestamp": p["timestamp"], "pnlUsd": pnl_usd, "pnlPct": pnl_pct})

    effective_window = pts[-1]["timestamp"] - pts[0]["timestamp"] if len(pts) >= 2 else 0

    info = {
        "step": step,
        "count": len(pts),
        "effectiveWindowSec": effective_window,
        "requestedWindowSec": requested_window,
        "ageSec": age,
    }
    series = {"tvl": tvl_series, "pnl": pnl_series}
    return info, series

app = FastAPI(title="backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(x_router)

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}

@app.get("/stats/{token}")
def stats_for_token(token: str) -> Dict[str, Dict[str, Any]]:
    snap = SEQUENCER._state.snapshot(token.lower())
    return {LABEL[h]: snap[h] for h in INTERVALS}

@app.get("/stats")
def stats_batch(tokens: List[str] = Query(...)) -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for t in tokens:
        snap = SEQUENCER._state.snapshot(t.lower())
        out[t.lower()] = {LABEL[h]: snap[h] for h in INTERVALS}
    return out

@app.get("/debug/tokens")
def debug_tokens() -> Dict[str, int]:
    return SEQUENCER._state.debug_tokens()

@app.get("/vaults/{address}/{user}/{timeframe}")
def vault_combined(
    address: str,
    user: str,
    timeframe: int,
    limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    vaddr = address.lower()
    uaddr = user.lower()

    st = SEQUENCER._state
    v = st.vaults.get(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")

    meta = {
        "vault": v.vault,
        "owner": v.owner,
        "quote": v.quote,
        "base": v.base,
        "market": v.market,
        "name": v.name,
        "description": v.description,
        "socials": {"social1": v.social1, "social2": v.social2, "social3": v.social3},
        "decimals": {"quoteDecimals": int(v.quoteDecimals), "baseDecimals": int(v.baseDecimals)},
        "params": {"maxShares": int(v.maxShares), "circulatingShares": int(v.circulatingShares)},
        "timestamp": int(getattr(v, "timestamp", 0) or 0),
    }
    status = {"locked": bool(v.locked), "closed": bool(v.closed)}

    latest = _latest_balance_for(vaddr)

    circ = int(v.circulatingShares) if v.circulatingShares is not None else 0
    uobj = st.vaultToUsers.get(vaddr, {}).get(uaddr)
    u_shares = int(uobj.shares) if uobj else 0
    if circ > 0 and u_shares > 0:
        share_pct = u_shares / circ
        user_quote = int(latest["quoteBalance"] * share_pct)
        user_base = int(latest["baseBalance"] * share_pct)
    else:
        share_pct = 0.0
        user_quote = 0
        user_base = 0
    user_balance = {"address": uaddr, "shares": u_shares, "sharePct": share_pct, "quoteBalance": user_quote, "baseBalance": user_base}

    deps = st.vaultToDeposits.get(vaddr, [])
    wds = st.vaultToWithdraws.get(vaddr, [])

    dep_hist = [{
        "user": d.user,
        "timestamp": int(d.timestamp),
        "quoteAmount": int(d.quoteAmount),
        "baseAmount": int(d.baseAmount),
        "shares": int(d.shares),
        "hash": d.hash,
    } for d in reversed(deps[-limit:])]

    wdr_hist = [{
        "user": w.user,
        "timestamp": int(w.timestamp),
        "quoteAmount": int(w.quoteAmount),
        "baseAmount": int(w.baseAmount),
        "shares": int(w.shares),
        "hash": w.hash,
    } for w in reversed(wds[-limit:])]

    depos_map = st.vaultToUsers.get(vaddr, {})
    circ_safe = circ if circ > 0 else 1
    depos_list = sorted(depos_map.values(), key=lambda x: int(x.lastDeposit or 0), reverse=True)
    depos_out = [{
        "address": d.address,
        "shares": int(d.shares),
        "sharePct": (d.shares / circ_safe),
        "deposits": int(d.deposits),
        "withdraws": int(d.withdraws),
        "lastDeposit": int(d.lastDeposit or 0),
        "lastWithdraw": int(d.lastWithdraw or 0),
    } for d in depos_list]

    info, series = _build_history(v, timeframe, limit)

    return {
        "ok": True,
        "vault": meta,
        "status": status,
        "latestBalance": latest,
        "tvlUsd": latest["usdValue"],
        "userBalance": user_balance,
        "depositHistory": dep_hist,
        "withdrawHistory": wdr_hist,
        "depositors": depos_out,
        "history": {
            "info": info,
            "series": series,
        },
    }

@app.get("/vaults/list")
def list_vaults(limit: int = Query(100, ge=1, le=1000)) -> Dict[str, Any]:
    st = SEQUENCER._state

    rows = []
    for vaddr, v in st.vaults.items():
        latest = _latest_balance_for(vaddr)
        rows.append({
            "vault": v.vault,
            "name": v.name,
            "owner": v.owner,
            "quote": v.quote,
            "base": v.base,
            "locked": bool(v.locked),
            "closed": bool(v.closed),
            "circulatingShares": int(v.circulatingShares or 0),
            "quoteDecimals": int(v.quoteDecimals or 0),
            "baseDecimals": int(v.baseDecimals or 0),
            "timestamp": int(getattr(v, "timestamp", 0) or 0),
            "latest": latest,
            "tvlUsd": latest["usdValue"],
        })

    rows.sort(key=lambda r: (r["tvlUsd"], r["latest"]["timestamp"]), reverse=True)
    return {"ok": True, "count": min(limit, len(rows)), "vaults": rows[:limit]}
