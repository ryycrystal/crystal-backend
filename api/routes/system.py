from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter

from api.api import _mon_price_usd, storage

router = APIRouter()


# the cached mon usd reference price used across the backend
@router.get("/debug/mon_price")
def get_mon_price() -> Decimal:
    return _mon_price_usd()


# last processed chain block, for sync checks
@router.get("/sync")
def get_sync_status() -> dict[str, Any]:
    last_block = storage.get_last_processed_block()
    return {
        "last_block": last_block,
    }


# simple health response for uptime checks
@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}
