from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter

from api.api import _WEI, _fmt, _fmt_usd, _mon_price_usd, storage
from modules import revenue

router = APIRouter()


@router.get("/debug/mon_price")
def get_mon_price() -> Decimal:
    return _mon_price_usd()


@router.get("/sync")
def get_sync_status() -> dict[str, Any]:
    last_block = storage.get_last_processed_block()
    return {
        "last_block": last_block,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


def _revenue_block(days: int) -> dict[str, Any]:
    now_ts = int(time.time())
    t = storage.revenue_totals(now_ts)
    mon_price = _mon_price_usd()

    def native(v):
        return Decimal(v) / _WEI

    out = {
        "feeAddress": revenue.CRYSTAL_FEE_ADDRESS,
        "balanceNative": _fmt(native(t["balance_native"])),
        "balanceUsd": _fmt_usd(native(t["balance_native"]) * mon_price),
        "trackedNative": _fmt(native(t["tracked_native"])),
        "trackedUsd": _fmt_usd(t["tracked_usd"]),
        "native24h": _fmt(native(t["native_24h"])),
        "usd24h": _fmt_usd(t["usd_24h"]),
        "native7d": _fmt(native(t["native_7d"])),
        "usd7d": _fmt_usd(t["usd_7d"]),
        "samples": t["samples"],
        "lastSampleAt": t["last_timestamp"],
        "lastSampleBlock": t["last_block"],
    }
    if days > 0:
        rows = storage.list_revenue_samples(now_ts - min(days, 90) * 86400, limit=2000)
        out["series"] = [
            {
                "block": int(b),
                "time": int(ts),
                "balanceNative": _fmt(native(bal)),
                "deltaNative": _fmt(native(dw)),
                "deltaUsd": _fmt_usd(du),
            }
            for b, ts, bal, dw, du in rows
        ]
    return out


@router.get("/integrity")
def get_integrity(revenue_days: int = 0) -> dict[str, Any]:
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
        "revenue": _revenue_block(max(0, int(revenue_days or 0))),
    }
