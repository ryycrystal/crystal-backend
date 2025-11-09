from __future__ import annotations
from typing import Dict, Any, List, Deque
from decimal import Decimal

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from core.sequencer import SEQUENCER
from state import INTERVALS, LABEL
from api.x_api import router as x_router
import models

_TIMEFRAME_MAP = {
    1: "day",
    2: "week",
    3: "month",
    4: "all",
}

def _pick_bucket(vaddr: str) -> Dict[int, Deque[models.VaultBalance]]:
    st = SEQUENCER._state
    return {
        1: st.vaultBalancesDay.get(vaddr, None),
        2: st.vaultBalancesWeek.get(vaddr, None),
        3: st.vaultBalancesMonth.get(vaddr, None),
        4: st.vaultBalancesAllTime.get(vaddr, None),
    }

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

@app.get("/vaults/{address}/{user}")
def vault_user_summary(
    address: str,
    user: str,
    history_limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    vaddr = address.lower()
    uaddr = user.lower()

    st = SEQUENCER._state

    v = st.vaults.get(vaddr)
    if not v:
        return {"ok": False, "error": "vault not found", "vault": vaddr}
    
    def _as_bal_dict(x):
        if isinstance(x, dict):
            return x
        return {
            "block": int(getattr(x, "block", 0)),
            "timestamp": int(getattr(x, "timestamp", 0)),
            "quoteBalance": int(getattr(x, "quoteBalance", 0)),
            "baseBalance": int(getattr(x, "baseBalance", 0)),
            "usdValue": float(getattr(x, "usdValue", 0.0)),
        }

    def _latest_balance():
        dq = SEQUENCER._state.vaultBalancesAllTime.get(vaddr, None)
        if not dq:
            return {"quoteBalance": 0, "baseBalance": 0, "timestamp": 0, "usdValue": 0.0}
        row = _as_bal_dict(dq[-1])
        return {
            "quoteBalance": int(row.get("quoteBalance", 0)),
            "baseBalance": int(row.get("baseBalance", 0)),
            "timestamp": int(row.get("timestamp", 0)),
            "usdValue": float(row.get("usdValue", 0.0)),
        }

    latest = _latest_balance()

    circ = int(v.circulatingShares) if v.circulatingShares is not None else 0
    usr_map = st.vaultToUsers.get(vaddr, {})
    uobj = usr_map.get(uaddr)
    u_shares = int(uobj.shares) if uobj else 0

    if circ > 0 and u_shares > 0:
        share_pct = u_shares / circ
        user_quote = int(latest["quoteBalance"] * share_pct)
        user_base = int(latest["baseBalance"] * share_pct)
    else:
        share_pct = 0.0
        user_quote = 0
        user_base = 0

    deps = st.vaultToDeposits.get(vaddr, [])
    wds = st.vaultToWithdraws.get(vaddr, [])

    dep_hist = [
        {
            "user": d.user,
            "timestamp": int(d.timestamp),
            "quoteAmount": int(d.quoteAmount),
            "baseAmount": int(d.baseAmount),
            "shares": int(d.shares),
            "hash": d.hash,
        }
        for d in reversed(deps[-history_limit:])
    ]

    wdr_hist = [
        {
            "user": w.user,
            "timestamp": int(w.timestamp),
            "quoteAmount": int(w.quoteAmount),
            "baseAmount": int(w.baseAmount),
            "shares": int(w.shares),
            "hash": w.hash,
        }
        for w in reversed(wds[-history_limit:])
    ]

    depos_map = st.vaultToUsers.get(vaddr, {})
    depos_list = sorted(
        depos_map.values(),
        key=lambda x: int(x.lastDeposit or 0),
        reverse=True,
    )
    depos_out = []
    for d in depos_list:
        pct = (d.shares / circ) if circ > 0 else 0.0
        depos_out.append(
            {
                "address": d.address,
                "shares": int(d.shares),
                "sharePct": pct,
                "deposits": int(d.deposits),
                "withdraws": int(d.withdraws),
                "lastDeposit": int(d.lastDeposit or 0),
                "lastWithdraw": int(d.lastWithdraw or 0),
            }
        )

    meta = {
        "vault": v.vault,
        "owner": v.owner,
        "quote": v.quote,
        "base": v.base,
        "market": v.market,
        "name": v.name,
        "description": v.description,
        "socials": {
            "social1": v.social1,
            "social2": v.social2,
            "social3": v.social3,
        },
        "decimals": {
            "quoteDecimals": int(v.quoteDecimals),
            "baseDecimals": int(v.baseDecimals),
        },
        "params": {
            "maxShares": int(v.maxShares),
            "circulatingShares": int(v.circulatingShares),
        },
    }

    status = {
        "locked": bool(v.locked),
        "closed": bool(v.closed),
    }

    user_balance = {
        "address": uaddr,
        "shares": u_shares,
        "sharePct": share_pct,
        "quoteBalance": user_quote,
        "baseBalance": user_base,
    }

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
    }

@app.get("/vaults/{address}/history/{timeframe}")
def vault_history(
    address: str,
    timeframe: int,
    limit: int = Query(0, ge=0, le=2000),
) -> Dict[str, Any]:
    vaddr = address.lower()
    st = SEQUENCER._state
    v = st.vaults.get(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")

    if timeframe not in _TIMEFRAME_MAP:
        raise HTTPException(status_code=400, detail="invalid timeframe")

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

    buckets = _pick_bucket(vaddr)
    dq = buckets.get(timeframe)
    if not dq or len(dq) == 0:
        return {
            "ok": True,
            "meta": {
                "vault": v.vault,
                "name": v.name,
                "owner": v.owner,
                "quote": v.quote,
                "base": v.base,
                "locked": v.locked,
                "closed": v.closed,
            },
            "series": {"tvl": [], "pnl": []},
            "info": {"timeframe": _TIMEFRAME_MAP[timeframe], "count": 0},
        }

    pts_raw = list(dq)[-limit:] if limit and limit > 0 else list(dq)
    pts = [_row_to_dict(b) for b in pts_raw]

    tvl_series: List[Dict[str, Any]] = [
        {"timestamp": p["timestamp"], "tvlUsd": p["usdValue"]} for p in pts
    ]

    base_usd = pts[0]["usdValue"] if pts else 0.0
    pnl_series: List[Dict[str, Any]] = []
    for p in pts:
        cur = p["usdValue"]
        pnl_usd = cur - base_usd
        pnl_pct = ((cur / base_usd) - 1.0) * 100.0 if base_usd > 0 else 0.0
        pnl_series.append({
            "timestamp": p["timestamp"],
            "pnlUsd": pnl_usd,
            "pnlPct": pnl_pct,
        })

    return {
        "ok": True,
        "meta": {
            "vault": v.vault,
            "name": v.name,
            "owner": v.owner,
            "quote": v.quote,
            "base": v.base,
            "locked": v.locked,
            "closed": v.closed,
            "circulatingShares": int(v.circulatingShares),
        },
        "series": {"tvl": tvl_series, "pnl": pnl_series},
        "info": {"timeframe": _TIMEFRAME_MAP[timeframe], "count": len(pts)},
    }
