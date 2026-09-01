from __future__ import annotations

import json
import os
import time
import urllib.request
from decimal import Decimal
from typing import Any

import core.storage as storage
from core.storage import db_cursor

_BALANCE_TTL_SECONDS = 3.0
_BALANCEOF_SELECTOR = "0x70a08231"

_balance_cache: dict[str, tuple[float, int, dict[str, int], int]] = {}

_native_cache: dict[str, tuple[float, int, int]] = {}

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
NATIVE = "native"


def spot_token_list() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (addr) addr, ticker, name, decimals FROM (
                SELECT LOWER(base_address) AS addr, base_ticker AS ticker,
                       base_name AS name, base_decimals AS decimals, updated_at
                FROM crystal_markets
                UNION ALL
                SELECT LOWER(quote_address), quote_ticker, quote_name, quote_decimals, updated_at
                FROM crystal_markets
            ) u
            ORDER BY addr, updated_at DESC NULLS LAST
            """
        )
        rows = cur.fetchall()
    out = [{"address": a, "ticker": t or "", "name": n or "", "decimals": int(d or 18)} for a, t, n, d in rows]
    out.append({"address": NATIVE, "ticker": "MON", "name": "Monad", "decimals": 18})
    return out


def spot_prices(mon_usd: Decimal) -> dict[str, Decimal | None]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT LOWER(base_address), LOWER(quote_address), last_price
            FROM crystal_markets
            WHERE last_price > 0
            ORDER BY updated_at ASC NULLS FIRST
            """
        )
        rows = cur.fetchall()

    native_equiv = {WMON}
    prices: dict[str, Decimal | None] = {
        WMON: mon_usd if mon_usd > 0 else None,
        NATIVE: mon_usd if mon_usd > 0 else None,
    }
    for base, quote, lp in rows:
        if quote in native_equiv:
            prices.setdefault(base, (Decimal(lp) * mon_usd) if mon_usd > 0 else None)
            if mon_usd > 0:
                prices[base] = Decimal(lp) * mon_usd
        elif base in native_equiv and Decimal(lp) > 0 and mon_usd > 0:
            prices.setdefault(quote, mon_usd / Decimal(lp))
    return prices


_known_wallets: set[str] = set()
_unknown_checked: dict[str, float] = {}
_UNKNOWN_TTL_SECONDS = 30.0


def wallet_is_supported(wallet: str) -> bool:
    from core.storage import wallet_has_crystal_activity

    w = (wallet or "").lower()
    if w in _known_wallets:
        return True
    now = time.monotonic()
    checked = _unknown_checked.get(w)
    if checked is not None and now - checked < _UNKNOWN_TTL_SECONDS:
        return False
    if wallet_has_crystal_activity(w):
        _known_wallets.add(w)
        _unknown_checked.pop(w, None)
        return True
    _unknown_checked[w] = now
    return False


def spot_body(wallet, include_zero: bool = False) -> dict[str, Any]:
    from api.api import _fmt, _fmt_usd, _mon_price_usd

    wallets = [(wallet or "").lower()] if isinstance(wallet, str) else [str(w).lower() for w in (wallet or [])]
    wallets = [w for w in dict.fromkeys(wallets) if w]
    wallet = wallets[0] if wallets else ""
    supported = [w for w in wallets if wallet_is_supported(w)]
    if not supported:
        return {
            "wallet": wallet,
            "wallets": wallets,
            "supported": False,
            "rows": [],
            "vaults": [],
            "liquidity": [],
            "summary": {
                "totalAccountValue": None,
                "firstActivityTs": None,
                "walletValue": None,
                "ordersValue": None,
                "vaultsValue": None,
                "liquidityValue": None,
                "totalVolume": None,
                "buySellRatio": None,
                "activeOrders": None,
                "changePct": None,
                "high": None,
                "low": None,
            },
            "balance_block": None,
            "stale": False,
        }

    tokens = spot_token_list()
    token_addrs = [t["address"] for t in tokens]
    balances: dict[str, int] = {}
    native_raw = 0
    balance_block = 0
    stale = False
    for w in supported:
        blk, bals, native, w_stale = fetch_balances(w, token_addrs)
        for addr, raw in (bals or {}).items():
            balances[addr] = balances.get(addr, 0) + int(raw or 0)
        native_raw += int(native or 0)
        balance_block = max(balance_block, int(blk or 0))
        stale = stale or bool(w_stale)

    locked = storage.open_order_locked_by_token(supported)
    vaults_total, vault_rows = storage.vault_positions_usd(supported)
    lp_total, lp_rows = storage.lp_positions_usd(supported)

    prices = spot_prices(_mon_price_usd())
    rows = []
    wallet_total = Decimal(0)
    orders_total = Decimal(0)
    for t in tokens:
        addr = t["address"]
        raw = native_raw if addr == NATIVE else balances.get(addr, 0)
        locked_raw = locked.get(addr, 0)
        if raw <= 0 and locked_raw <= 0 and not include_zero:
            continue
        unit = Decimal(10) ** int(t["decimals"])
        bal = Decimal(raw) / unit
        locked_bal = Decimal(locked_raw) / unit
        price = prices.get(addr)
        value = (bal * price) if price is not None else None
        locked_value = (locked_bal * price) if price is not None else None
        if value is not None:
            wallet_total += value
        if locked_value is not None:
            orders_total += locked_value
        combined = None if price is None else (value or Decimal(0)) + (locked_value or Decimal(0))
        rows.append(
            {
                "address": addr,
                "ticker": t["ticker"],
                "name": t["name"],
                "decimals": t["decimals"],
                "balanceRaw": str(raw),
                "balance": _fmt(bal),
                "lockedRaw": str(locked_raw),
                "locked": _fmt(locked_bal),
                "lockedValueUsd": _fmt_usd(locked_value) if locked_value is not None else None,
                "priceUsd": _fmt_usd(price) if price is not None else None,
                "priceChange24h": None,
                "valueUsd": _fmt_usd(value) if value is not None else None,
                "totalValueUsd": _fmt_usd(combined) if combined is not None else None,
            }
        )
    mon_usd = prices.get(NATIVE) or Decimal(0)
    seen_addrs = {r["address"] for r in rows}
    for g in storage.graduated_holdings(supported):
        if g["token"] in seen_addrs:
            continue
        bal = Decimal(int(g["balance_raw"])) / Decimal(10) ** 18
        price = (Decimal(g["last_price_native"]) * mon_usd) if mon_usd > 0 else None
        value = (bal * price) if price is not None else None
        if value is not None:
            wallet_total += value
        rows.append(
            {
                "address": g["token"],
                "ticker": g["symbol"],
                "name": g["name"],
                "decimals": 18,
                "balanceRaw": str(int(g["balance_raw"])),
                "balance": _fmt(bal),
                "lockedRaw": "0",
                "locked": _fmt(Decimal(0)),
                "lockedValueUsd": None,
                "priceUsd": _fmt_usd(price) if price is not None else None,
                "priceChange24h": None,
                "valueUsd": _fmt_usd(value) if value is not None else None,
                "totalValueUsd": _fmt_usd(value) if value is not None else None,
            }
        )
    rows.sort(key=lambda r: Decimal(r["totalValueUsd"] or r["valueUsd"] or 0), reverse=True)
    total = wallet_total + orders_total + vaults_total + lp_total

    return {
        "wallet": wallet,
        "wallets": supported,
        "supported": True,
        "rows": rows,
        "vaults": [
            {**v, "valueUsd": _fmt_usd(v["valueUsd"]) if v["valueUsd"] is not None else None} for v in vault_rows
        ],
        "liquidity": [
            {**lp, "valueUsd": _fmt_usd(lp["valueUsd"]) if lp["valueUsd"] is not None else None} for lp in lp_rows
        ],
        "summary": {
            "totalAccountValue": _fmt_usd(total),
            "firstActivityTs": storage.wallet_first_activity_ts(supported),
            "walletValue": _fmt_usd(wallet_total),
            "ordersValue": _fmt_usd(orders_total),
            "vaultsValue": _fmt_usd(vaults_total),
            "liquidityValue": _fmt_usd(lp_total),
            "totalVolume": None,
            "buySellRatio": None,
            "activeOrders": None,
            "changePct": None,
            "high": None,
            "low": None,
        },
        "balance_block": balance_block,
        "stale": stale,
    }


def fetch_native_balance(wallet: str) -> tuple[int, int, bool]:
    wallet = wallet.lower()
    now = time.monotonic()
    cached = _native_cache.get(wallet)
    if cached and now - cached[0] < _BALANCE_TTL_SECONDS:
        return cached[1], cached[2], False

    rpc = os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    batch = [
        {"jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber", "params": []},
        {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [wallet, "latest"]},
    ]
    try:
        req = urllib.request.Request(rpc, data=json.dumps(batch).encode(), headers={"Content-Type": "application/json"})
        results = json.load(urllib.request.urlopen(req, timeout=15))
        by_id = {r.get("id"): r.get("result") for r in results if isinstance(r, dict)}
        block = int(by_id.get(0) or "0x0", 16)
        native = int(by_id.get(1) or "0x0", 16)
        _native_cache[wallet] = (now, block, native)
        return block, native, False
    except Exception:
        if cached:
            return cached[1], cached[2], True
        raise


def fetch_balances(wallet: str, tokens: list[str]) -> tuple[int, dict[str, int], int, bool]:
    wallet = wallet.lower()
    now = time.monotonic()
    cached = _balance_cache.get(wallet)
    if cached and now - cached[0] < _BALANCE_TTL_SECONDS:
        return cached[1], cached[2], cached[3], False

    rpc = os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    arg = wallet[2:].rjust(64, "0")
    batch: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber", "params": []},
        {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [wallet, "latest"]},
    ]
    erc20 = [t for t in tokens if t != NATIVE]
    for i, tok in enumerate(erc20):
        batch.append(
            {
                "jsonrpc": "2.0",
                "id": 2 + i,
                "method": "eth_call",
                "params": [{"to": tok, "data": _BALANCEOF_SELECTOR + arg}, "latest"],
            }
        )
    try:
        req = urllib.request.Request(rpc, data=json.dumps(batch).encode(), headers={"Content-Type": "application/json"})
        results = json.load(urllib.request.urlopen(req, timeout=15))
        by_id = {r.get("id"): r.get("result") for r in results if isinstance(r, dict)}
        block = int(by_id.get(0) or "0x0", 16)
        native = int(by_id.get(1) or "0x0", 16)
        balances = {}
        for i, tok in enumerate(erc20):
            res = by_id.get(2 + i)
            balances[tok] = int(res, 16) if isinstance(res, str) and res.startswith("0x") and res != "0x" else 0
        _balance_cache[wallet] = (now, block, balances, native)
        return block, balances, native, False
    except Exception:
        if cached:
            return cached[1], cached[2], cached[3], True
        raise
