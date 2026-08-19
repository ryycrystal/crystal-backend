from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

import core.storage as storage
from core.storage import db_cursor

router = APIRouter()


def _pool_row_to_api(row) -> dict[str, Any]:
    from api.api import _crystal_pool_row_to_api

    return _crystal_pool_row_to_api(row)


def _sample_pool_chart_points(points: list[dict[str, Any]], max_points: int = 48) -> list[dict[str, Any]]:
    from api.api import _sample_evenly_by_time

    return _sample_evenly_by_time(points, max_points, lambda p: int((p or {}).get("timestamp") or 0))


# endpoint for pools list used by /earn/liquidity
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


# endpoint for complete info for one pool
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
    # real historical apy per chart point: fees actually earned in the trailing 24h
    # window before each point, annualized against the tvl at that point. the old
    # series repeated the current apy across every timestamp, a chart shaped like a
    # ruler no matter what the pool did
    out["apyHistory"] = _apy_history(out["market"], tvl_history)

    return out


# annualized apy at each tvl point from the fees ledger, null when tvl is unknown
def _apy_history(market: str, tvl_history: list[dict]) -> list[dict]:
    if not tvl_history:
        return []
    lo = int(tvl_history[0]["timestamp"]) - 86400
    hi = int(tvl_history[-1]["timestamp"])
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, COALESCE(fees_usd, 0)
            FROM crystal_pool_sync_events
            WHERE market = %s AND timestamp BETWEEN %s AND %s AND COALESCE(fees_usd, 0) > 0
            ORDER BY timestamp
            """,
            ((market or "").lower(), lo, hi),
        )
        fee_events = cur.fetchall()

    out = []
    j = 0
    window: list[tuple[int, float]] = []
    total = 0.0
    for p in tvl_history:
        ts = int(p["timestamp"])
        while j < len(fee_events) and int(fee_events[j][0]) <= ts:
            f = float(fee_events[j][1] or 0.0)
            window.append((int(fee_events[j][0]), f))
            total += f
            j += 1
        while window and window[0][0] < ts - 86400:
            total -= window.pop(0)[1]
        tvl = float(p.get("tvl") or 0.0)
        apy = (total / tvl) * 365.0 * 100.0 if tvl > 0 else None
        out.append({"timestamp": ts, "apy": apy})
    return out
