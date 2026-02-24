from __future__ import annotations
from decimal import Decimal
from typing import Dict, Any
from fastapi import APIRouter
from api.api import _mon_price_usd, storage

router = APIRouter()

# Return the cached MON/USD reference price used by the backend
@router.get("/debug/mon_price")
def get_mon_price() -> Decimal:
    return(_mon_price_usd())


# Return the last processed chain block for indexer sync checks
@router.get("/sync")
def get_sync_status() -> Dict[str, Any]:
    last_block = storage.get_last_processed_block()
    return {
        "last_block": last_block,
    }


# Return a simple health response for uptime checks
@router.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}
