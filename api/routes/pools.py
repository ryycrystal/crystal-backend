from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import core.storage as storage

router = APIRouter()


def _pool_row_to_api(row) -> dict[str, Any]:
    from api.api import _crystal_pool_row_to_api

    return _crystal_pool_row_to_api(row)


def _sample_pool_chart_points(points: list[dict[str, Any]], max_points: int = 48) -> list[dict[str, Any]]:
    from api.api import _sample_evenly_by_time

    return _sample_evenly_by_time(points, max_points, lambda p: int((p or {}).get("timestamp") or 0))


@router.get("/pools/list")
def list_pools(
    search: str | None = Query(None, min_length=0, max_length=128),
    tokens: str | None = Query(None, description="optional comma-separated token addresses (1-2)"),
    sort: str = Query("volume", description="volume | tvl | apy"),
    order: str = Query("desc", description="asc | desc"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=50),
) -> dict[str, Any]:
    search_norm = search if isinstance(search, str) else ""
    sort_norm = (sort if isinstance(sort, str) else "volume" or "volume").strip().lower()
    if sort_norm not in {"volume", "tvl", "apy"}:
        raise HTTPException(status_code=400, detail="invalid sort")
    order_norm = (order if isinstance(order, str) else "desc" or "desc").strip().lower()
    if order_norm not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="invalid order")
    try:
        page_i = int(page)
    except Exception:
        page_i = 1
    try:
        limit_i = int(limit)
    except Exception:
        limit_i = 50
    token_filters: list[str] = []
    if isinstance(tokens, str) and tokens.strip():
        for part in tokens.split(","):
            t = (part or "").strip().lower()
            if not t:
                continue
            token_filters.append(t)
        token_filters = token_filters[:2]
    rows = storage.list_crystal_pools_with_state(
        search=search_norm,
        token_addresses=token_filters,
        page=page_i,
        limit=limit_i,
        sort_by=sort_norm,
        sort_dir=order_norm,
    )
    total = int(rows[0][25] or 0) if rows and len(rows[0]) > 25 else 0

    return {
        "ok": True,
        "pools": [_pool_row_to_api(r) for r in rows],
        "count": len(rows),
        "total": total,
        "page": page_i,
        "limit": limit_i,
        "hasMore": (page_i * limit_i) < total,
        "sort": sort_norm,
        "order": order_norm,
        "search": search_norm,
        "tokens": token_filters,
    }


@router.get("/pools/{address}")
def get_pool(
    address: str,
    history_seconds: int = Query(24 * 3600, ge=3600, le=365 * 24 * 3600),
    history_limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    try:
        history_seconds_i = int(history_seconds)
    except Exception:
        history_seconds_i = 7 * 24 * 3600
    try:
        history_limit_i = int(history_limit)
    except Exception:
        history_limit_i = 500
    row = storage.get_crystal_pool_with_state(address)
    if not row:
        raise HTTPException(status_code=404, detail="pool not found")
    out = _pool_row_to_api(row)
    latest_ts = int(out.get("updatedAt") or 0)
    since_ts = max(0, latest_ts - history_seconds_i) if latest_ts > 0 and history_seconds_i > 0 else None
    samples = storage.list_crystal_pool_tvl_samples(
        out["market"],
        since_ts=since_ts,
        limit=history_limit_i,
    )
    tvl_history = [{"timestamp": int(ts or 0), "tvl": float(v or 0.0)} for ts, v in samples]
    tvl_history = _sample_pool_chart_points(tvl_history, 48)
    out["tvlHistory"] = tvl_history
    out["apyHistory"] = _apy_history(out["market"], tvl_history)

    return out


def _apy_history(market: str, tvl_history: list[dict]) -> list[dict]:
    if not tvl_history:
        return []
    lo = int(tvl_history[0]["timestamp"]) - 86400
    hi = int(tvl_history[-1]["timestamp"])
    events = storage.list_pool_growth_events(market, lo, hi)

    out = []
    j = 0
    window: list[tuple[int, float]] = []
    total_log = 0.0
    for p in tvl_history:
        ts = int(p["timestamp"])
        while j < len(events) and int(events[j][0]) <= ts:
            g = float(events[j][1] or 0.0)
            window.append((int(events[j][0]), g))
            total_log += g
            j += 1
        while window and window[0][0] < ts - 86400:
            total_log -= window.pop(0)[1]
        tvl = float(p.get("tvl") or 0.0)
        apy = (math.exp(total_log) - 1.0) * 365.0 * 100.0 if tvl > 0 else None
        out.append({"timestamp": ts, "apy": apy})
    return out


@router.get("/pools/positions/{user_addr}")
def lp_positions(user_addr: str) -> dict[str, Any]:
    user_addr = (user_addr or "").lower()
    if not user_addr.startswith("0x") or len(user_addr) != 42:
        raise HTTPException(status_code=400, detail="invalid address")
    rows = storage.list_lp_positions(user_addr)
    out = []
    for market, shares, last_transfer, cost_q, cost_b in rows:
        pool_row = storage.get_crystal_pool_with_state(market)
        pool = _pool_row_to_api(pool_row) if pool_row else {"market": market}
        total = int(pool.get("totalShares") or 0)
        share_pct = (shares / total * 100.0) if total > 0 else None
        rq = int(pool.get("reserveQuote") or 0)
        rb = int(pool.get("reserveBase") or 0)
        cur_q = shares * rq // total if total > 0 else 0
        cur_b = shares * rb // total if total > 0 else 0
        out.append(
            {
                "market": market,
                "shares": str(shares),
                "sharePct": share_pct,
                "lastTransfer": last_transfer,
                "currentQuote": str(cur_q),
                "currentBase": str(cur_b),
                "costQuote": str(cost_q),
                "costBase": str(cost_b),
                "earnedQuoteEst": str(cur_q - cost_q),
                "earnedBaseEst": str(cur_b - cost_b),
                "pool": pool,
            }
        )
    return {"ok": True, "user": user_addr, "count": len(out), "positions": out}


@router.get("/pools/{pool_addr}/liquidity")
def pool_liquidity_history(pool_addr: str, user: str = Query(""), limit: int = Query(100, le=500)) -> dict[str, Any]:
    pool_addr = (pool_addr or "").lower()
    events = storage.list_pool_liquidity_events(pool_addr, user=user, limit=limit)
    return {"ok": True, "market": pool_addr, "count": len(events), "events": events}


@router.get("/pools/{pool_addr}/preview")
def pool_preview(
    pool_addr: str,
    quote: int = Query(0, ge=0),
    base: int = Query(0, ge=0),
    shares: int = Query(0, ge=0),
) -> dict[str, Any]:
    pool_addr = (pool_addr or "").lower()
    row = storage.get_crystal_pool_with_state(pool_addr)
    if not row:
        raise HTTPException(status_code=404, detail="pool not found")
    pool = _pool_row_to_api(row)
    rq = int(pool.get("reserveQuote") or 0)
    rb = int(pool.get("reserveBase") or 0)
    total = int(pool.get("totalShares") or 0)

    if shares > 0:
        if total <= 0:
            raise HTTPException(status_code=400, detail="pool has no shares")
        s = min(shares, total)
        return {
            "ok": True,
            "kind": "withdraw",
            "shares": str(s),
            "amountQuoteOut": str(s * rq // total),
            "amountBaseOut": str(s * rb // total),
            "as_of_block": pool.get("lastSyncBlock"),
        }

    if quote <= 0 or base <= 0:
        raise HTTPException(status_code=400, detail="deposit preview needs quote and base, withdraw needs shares")
    if rq <= 0 or rb <= 0 or total <= 0:
        from decimal import Decimal

        lp = int((Decimal(quote) * Decimal(base)).sqrt())
        return {
            "ok": True,
            "kind": "deposit",
            "amountQuoteUsed": str(quote),
            "amountBaseUsed": str(base),
            "lpOut": str(lp),
            "firstDeposit": True,
            "as_of_block": pool.get("lastSyncBlock"),
        }

    base_optimal = quote * rb // rq
    if base_optimal <= base:
        used_q, used_b = quote, base_optimal
    else:
        used_q, used_b = base * rq // rb, base
    lp = min(used_q * total // rq, used_b * total // rb)
    return {
        "ok": True,
        "kind": "deposit",
        "amountQuoteUsed": str(used_q),
        "amountBaseUsed": str(used_b),
        "lpOut": str(lp),
        "firstDeposit": False,
        "as_of_block": pool.get("lastSyncBlock"),
    }
