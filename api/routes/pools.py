from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, HTTPException
from api.api import storage, _crystal_pool_row_to_api

router = APIRouter()


# Return indexed AMM pool markets in the legacy pools list response shape
@router.get("/pools/list")
def list_pools() -> Dict[str, Any]:
    rows = storage.list_crystal_pool_markets()
    return {"pools": [_crystal_pool_row_to_api(r) for r in rows]}


# Return one indexed AMM pool market in the legacy pool response shape
@router.get("/pools/{address}")
def get_pool(address: str) -> Dict[str, Any]:
    row = storage.get_crystal_pool_market(address)
    if not row:
        raise HTTPException(status_code=404, detail="pool not found")
    return _crystal_pool_row_to_api(row)
