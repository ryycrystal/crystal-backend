from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException

import core.storage as storage

router = APIRouter()

STALE_SECONDS = float(os.getenv("ORDERBOOK_STALE_SECONDS", "300"))
MAX_WALLETS = 16


def orderbook_data_is_stale() -> bool:
    if STALE_SECONDS <= 0:
        return False
    return time.time() - storage.latest_trade_timestamp() > STALE_SECONDS


def _wallet(addr: str) -> str:
    w = (addr or "").lower()
    if not w.startswith("0x") or len(w) != 42:
        raise HTTPException(status_code=400, detail="invalid wallet address")
    return w


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


def _limit(n: int) -> int:
    return max(1, min(int(n or 100), 500))


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


_KLINE_RESOLUTIONS = (60, 300, 900, 3600, 14400, 86400)


@router.get("/orderbook/klines/{market}")
def market_klines(market: str, res: int = 3600, limit: int = 3000) -> dict[str, Any]:
    m = (market or "").lower()
    if not m.startswith("0x") or len(m) != 42:
        raise HTTPException(status_code=400, detail="invalid market address")
    if int(res) not in _KLINE_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"res must be one of {_KLINE_RESOLUTIONS}")
    _ensure_fresh()
    lim = max(1, min(int(limit or 3000), 3000))
    rows = storage.market_klines(m, int(res), lim)
    return {
        "market": m,
        "res": int(res),
        "klines": rows,
        "count": len(rows),
        "as_of_block": int(storage.get_last_processed_block() or 0),
    }


_HEX = set("0123456789abcdef")


def _prefs_key(key: str) -> str:
    k = (key or "").strip().lower()
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


_RES_LADDER = (1, 5, 15, 60, 300, 900, 3600, 14400, 86400)
_RES_BUCKET_CAP = 5000


def coarsened_resolution(span_seconds: int, res: int) -> int:
    if span_seconds // max(res, 1) <= _RES_BUCKET_CAP:
        return res
    needed = span_seconds // _RES_BUCKET_CAP + 1
    for r in _RES_LADDER:
        if r >= needed:
            return r
    return _RES_LADDER[-1]


@router.get("/wallets/last-active")
def wallets_last_active(addresses: str = "") -> dict[str, Any]:
    addrs = [a.strip().lower() for a in (addresses or "").split(",") if a.strip()][:100]
    if not addrs:
        return {"lastActive": {}}
    return {"lastActive": storage.wallets_last_activity(addrs)}


@router.get("/activity/{wallet}")
def wallet_activity(
    wallet: str, limit: int = 50, before_ts: int | None = None, cursor: str = "", addresses: str = ""
) -> dict[str, Any]:
    from api.api import _decode_cursor, _encode_cursor

    ws = _wallets(wallet, addresses)
    lim = max(1, min(int(limit or 50), 200))

    before_key = None
    if cursor:
        try:
            c = _decode_cursor(cursor)
            before_key = (int(c["ts"]), int(c["blk"]), int(c["li"]), str(c["tx"]))
        except Exception:
            raise HTTPException(status_code=400, detail="bad cursor") from None

    items = storage.wallet_activity(ws, limit=lim, before_ts=before_ts, before_key=before_key)

    next_cursor = None
    if len(items) >= lim:
        last = items[-1]
        next_cursor = _encode_cursor(
            {
                "ts": int(last["timestamp"]),
                "blk": int(last["blockNumber"]),
                "li": int(last.get("logIndex") or 0),
                "tx": last["txhash"],
            }
        )

    return {
        "wallet": ws[0],
        "wallets": ws,
        "items": items,
        "count": len(items),
        "next_cursor": next_cursor,
        "next_before_ts": items[-1]["timestamp"] if len(items) >= lim else None,
    }
