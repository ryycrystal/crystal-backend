from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Query
import core.storage as storage

router = APIRouter()


def _pool_row_to_api(row) -> Dict[str, Any]:
    from api.api import _crystal_pool_row_to_api
    return _crystal_pool_row_to_api(row)


# Return indexed AMM pool markets in the legacy pools list response shape
@router.get("/pools/list")
def list_pools() -> Dict[str, Any]:
    rows = storage.list_crystal_pools_with_state()
    return {"pools": [_pool_row_to_api(r) for r in rows]}


# Return one indexed AMM pool market in the legacy pool response shape
@router.get("/pools/{address}")
def get_pool(
    address: str,
    history_seconds: int = Query(7 * 24 * 3600, ge=3600, le=365 * 24 * 3600),
    history_limit: int = Query(500, ge=1, le=2000),
) -> Dict[str, Any]:
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
    out["tvlHistory"] = tvl_history
    out["apyHistory"] = [
        {"timestamp": int(p["timestamp"]), "apy": float(out.get("apy24h") or 0.0)}
        for p in tvl_history
    ]
    return out
