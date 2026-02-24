from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter
from api.api import storage, _crystal_market_dump_row_to_api

router = APIRouter()


# Return a full dump of indexed Crystal markets for inspection and debugging
@router.get("/markets/list")
def list_markets_dump() -> Dict[str, Any]:
    rows = storage.list_crystal_markets_dump()
    markets = [_crystal_market_dump_row_to_api(r) for r in rows]
    return {"count": len(markets), "markets": markets}
