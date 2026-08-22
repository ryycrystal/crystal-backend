from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException

import core.storage as storage

router = APIRouter()

# refuse to serve orderbook state while the indexer is replaying history: an
# empty-but-200 answer would read as "no orders" and stop the client falling
# back, while a 503 sends it to the fallback until the replay reaches head
STALE_SECONDS = float(os.getenv("ORDERBOOK_STALE_SECONDS", "300"))


def orderbook_data_is_stale() -> bool:
    if STALE_SECONDS <= 0:
        return False
    return time.time() - storage.latest_trade_timestamp() > STALE_SECONDS


# a wallet path parameter, normalised or refused
def _wallet(addr: str) -> str:
    w = (addr or "").lower()
    if not w.startswith("0x") or len(w) != 42:
        raise HTTPException(status_code=400, detail="invalid wallet address")
    return w


def _ensure_fresh() -> None:
    if orderbook_data_is_stale():
        raise HTTPException(status_code=503, detail="indexer is catching up, serve from fallback")


# a page size clamped to something the db is happy to serve
def _limit(n: int) -> int:
    return max(1, min(int(n or 100), 500))


# open resting orders for one wallet, served from the decoded order state so the
# client stops reading order slots off the chain
@router.get("/orderbook/open/{wallet}")
def open_orders(wallet: str, market: str = "") -> dict[str, Any]:
    w = _wallet(wallet)
    _ensure_fresh()
    rows = storage.list_open_orders(w, market=(market or "").lower() or None)
    return {
        "wallet": w,
        "orders": rows,
        "count": len(rows),
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# every order a wallet has ever owned with its current status, the order
# history surface: places still live, cancels and fills already resolved
@router.get("/orderbook/orders/{wallet}")
def wallet_orders(
    wallet: str, market: str = "", limit: int = 200, before_ts: int | None = None
) -> dict[str, Any]:
    w = _wallet(wallet)
    _ensure_fresh()
    lim = max(1, min(int(limit or 200), 500))
    rows = storage.list_wallet_orders(w, market=(market or "").lower() or None, limit=lim, before_ts=before_ts)
    return {
        "wallet": w,
        "orders": rows,
        "count": len(rows),
        "next_before_ts": rows[-1]["updated_ts"] if len(rows) >= lim else None,
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# exchange trade history for one wallet: taker trades and maker fills merged,
# newest first. before_ts pages backwards through time
@router.get("/orderbook/trades/{wallet}")
def exchange_trades(
    wallet: str, market: str = "", limit: int = 100, before_ts: int | None = None
) -> dict[str, Any]:
    w = _wallet(wallet)
    _ensure_fresh()
    lim = _limit(limit)
    rows = storage.list_exchange_trades(w, market=(market or "").lower() or None, limit=lim, before_ts=before_ts)
    return {
        "wallet": w,
        "trades": rows,
        "count": len(rows),
        "next_before_ts": rows[-1]["timestamp"] if len(rows) >= lim else None,
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# order lifecycle history for one wallet: places, cancels, decreases and fills
@router.get("/orderbook/history/{wallet}")
def order_history(
    wallet: str, market: str = "", limit: int = 100, before_ts: int | None = None
) -> dict[str, Any]:
    w = _wallet(wallet)
    _ensure_fresh()
    lim = _limit(limit)
    rows = storage.list_order_history(w, market=(market or "").lower() or None, limit=lim, before_ts=before_ts)
    return {
        "wallet": w,
        "events": rows,
        "count": len(rows),
        "next_before_ts": rows[-1]["timestamp"] if len(rows) >= lim else None,
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }
