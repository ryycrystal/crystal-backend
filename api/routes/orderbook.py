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
# derived wallets are capped at ten per session, so a unified view never
# needs more than that plus the parent account
MAX_WALLETS = 16


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


# the set of wallets a request covers. a user trades from several derived
# wallets at once and wants one view across the ones they have selected, so
# every read accepts an optional list and falls back to the path wallet
def _wallets(wallet: str, addresses: str) -> list[str]:
    out: list[str] = []
    for a in (addresses or "").split(","):
        a = a.strip().lower()
        if a.startswith("0x") and len(a) == 42 and a not in out:
            out.append(a)
    if not out:
        return [_wallet(wallet)]
    if len(out) > MAX_WALLETS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_WALLETS} addresses")
    return out


def _ensure_fresh() -> None:
    if orderbook_data_is_stale():
        raise HTTPException(status_code=503, detail="indexer is catching up, serve from fallback")


# a page size clamped to something the db is happy to serve
def _limit(n: int) -> int:
    return max(1, min(int(n or 100), 500))


# open resting orders for one wallet, served from the decoded order state so the
# client stops reading order slots off the chain
@router.get("/orderbook/open/{wallet}")
def open_orders(wallet: str, market: str = "", addresses: str = "") -> dict[str, Any]:
    ws = _wallets(wallet, addresses)
    _ensure_fresh()
    rows = storage.list_open_orders(ws, market=(market or "").lower() or None)
    return {
        "wallet": ws[0],
        "wallets": ws,
        "orders": rows,
        "count": len(rows),
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# every order a wallet has ever owned with its current status, the order
# history surface: places still live, cancels and fills already resolved
@router.get("/orderbook/orders/{wallet}")
def wallet_orders(
    wallet: str, market: str = "", limit: int = 200, before_ts: int | None = None, addresses: str = ""
) -> dict[str, Any]:
    ws = _wallets(wallet, addresses)
    _ensure_fresh()
    lim = max(1, min(int(limit or 200), 500))
    rows = storage.list_wallet_orders(ws, market=(market or "").lower() or None, limit=lim, before_ts=before_ts)
    return {
        "wallet": ws[0],
        "wallets": ws,
        "orders": rows,
        "count": len(rows),
        "next_before_ts": rows[-1]["updated_ts"] if len(rows) >= lim else None,
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# exchange trade history for one wallet: taker trades and maker fills merged,
# newest first. before_ts pages backwards through time
@router.get("/orderbook/trades/{wallet}")
def exchange_trades(
    wallet: str, market: str = "", limit: int = 100, before_ts: int | None = None, addresses: str = ""
) -> dict[str, Any]:
    ws = _wallets(wallet, addresses)
    _ensure_fresh()
    lim = _limit(limit)
    rows = storage.list_exchange_trades(ws, market=(market or "").lower() or None, limit=lim, before_ts=before_ts)
    return {
        "wallet": ws[0],
        "wallets": ws,
        "trades": rows,
        "count": len(rows),
        "next_before_ts": rows[-1]["timestamp"] if len(rows) >= lim else None,
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# order lifecycle history for one wallet: places, cancels, decreases and fills
@router.get("/orderbook/history/{wallet}")
def order_history(
    wallet: str, market: str = "", limit: int = 100, before_ts: int | None = None, addresses: str = ""
) -> dict[str, Any]:
    ws = _wallets(wallet, addresses)
    _ensure_fresh()
    lim = _limit(limit)
    rows = storage.list_order_history(ws, market=(market or "").lower() or None, limit=lim, before_ts=before_ts)
    return {
        "wallet": ws[0],
        "wallets": ws,
        "events": rows,
        "count": len(rows),
        "next_before_ts": rows[-1]["timestamp"] if len(rows) >= lim else None,
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


# derived wallet preferences, so a session that adds or selects wallets on one
# device finds the same set on another. the key is opaque and client derived:
# the server stores a count and which indices are selected, never an address,
# never a key, and has no way to tell whose record it is
_HEX = set("0123456789abcdef")


def _prefs_key(key: str) -> str:
    k = (key or "").strip().lower()
    # a 64 char hash, never an address: refusing address shaped keys keeps a
    # caller from accidentally storing this under something identifying
    if len(k) != 64 or any(c not in _HEX for c in k):
        raise HTTPException(status_code=400, detail="key must be 64 hex characters")
    return k


@router.get("/wallet-prefs/{key}")
def read_wallet_prefs(key: str) -> dict[str, Any]:
    prefs = storage.get_wallet_prefs(_prefs_key(key))
    return prefs or {"count": 0, "selected": [], "updatedAt": 0}


@router.put("/wallet-prefs/{key}")
def write_wallet_prefs(key: str, body: dict[str, Any]) -> dict[str, Any]:
    k = _prefs_key(key)
    try:
        count = int(body.get("count") or 0)
        selected = [int(i) for i in (body.get("selected") or [])]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="count and selected must be integers") from None
    if not 0 <= count <= MAX_WALLETS or len(selected) > MAX_WALLETS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_WALLETS} wallets")
    if any(i < 0 or i >= MAX_WALLETS for i in selected):
        raise HTTPException(status_code=400, detail="selected holds wallet indices")
    updated_at = int(time.time())
    storage.put_wallet_prefs(k, count, sorted(set(selected)), updated_at)
    return {"count": count, "selected": sorted(set(selected)), "updatedAt": updated_at}
