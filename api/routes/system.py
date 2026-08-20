from __future__ import annotations

import json
import os
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


# the indexer's last self check plus live staleness, for data monitors
@router.get("/integrity")
def get_integrity() -> dict[str, Any]:
    raw = storage.get_meta("integrity_last")
    sweep = json.loads(raw) if raw else None
    with storage.db_cursor() as cur:
        cur.execute("SELECT MAX(number), EXTRACT(EPOCH FROM Now() - MAX(processed_at)) FROM launchpad_blocks")
        row = cur.fetchone()
    last_block = int(row[0]) if row and row[0] is not None else None
    stall = float(row[1] or 0.0) if row else 0.0
    stall_limit = int(os.getenv("INTEGRITY_STALL_LIMIT", "60"))
    ok = bool(sweep and sweep.get("ok")) and last_block is not None and stall < stall_limit
    return {
        "ok": ok,
        "last_block": last_block,
        "seconds_since_last_block": round(stall, 1),
        "sweep": sweep,
    }
