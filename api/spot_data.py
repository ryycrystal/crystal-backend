# data assembly for the spot portfolio: one call returns everything the tab renders.
#
# balances are read server side as a single json-rpc batch and cached briefly, so a
# wallet with many viewers costs one rpc read per cache window instead of one
# multicall per browser per 300ms. prices come from our own markets table crossed
# through the mon/usd oracle, which removes the subgraph dependency entirely

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

# wallet -> (fetched_at, block, {token -> raw balance}, native raw)
_balance_cache: dict[str, tuple[float, int, dict[str, int], int]] = {}

# wallet -> (fetched_at, block, native raw), separate from the full cache so a
# native-only read never poisons the token map for a spot call in the same window
_native_cache: dict[str, tuple[float, int, int]] = {}

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
NATIVE = "native"


# every asset listed on the exchange, derived from the markets table so listing a
# market automatically adds its tokens to the portfolio
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


# usd price per whole token for every listed asset, from our own market prices
# crossed through the mon/usd oracle. null when no market path to usd exists
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
    # direct: BASE/WMON markets price the base in native terms
    for base, quote, lp in rows:
        if quote in native_equiv:
            prices.setdefault(base, (Decimal(lp) * mon_usd) if mon_usd > 0 else None)
            if mon_usd > 0:
                prices[base] = Decimal(lp) * mon_usd
        elif base in native_equiv and Decimal(lp) > 0 and mon_usd > 0:
            # WMON/QUOTE market: the quote is priced by inversion
            prices.setdefault(quote, mon_usd / Decimal(lp))
    return prices


# wallets confirmed to have crystal activity never lose that status, unknown
# wallets are re-checked on a short ttl so a first trade flips them quickly
_known_wallets: set[str] = set()
_unknown_checked: dict[str, float] = {}
_UNKNOWN_TTL_SECONDS = 30.0


# true when the wallet has ever interacted with crystal, cached so the hot spot
# endpoint does not re-run the existence probes on every poll tick
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


# the whole spot body minus the graph, shared by the rest endpoint and the ws
# balances channel so the two can never disagree. raises only when the balance
# read fails cold with no cached snapshot to fall back on
def spot_body(wallet: str, include_zero: bool = False) -> dict[str, Any]:
    from api.api import _fmt, _fmt_usd, _mon_price_usd

    wallet = (wallet or "").lower()
    if not wallet_is_supported(wallet):
        return {
            "wallet": wallet,
            "supported": False,
            "rows": [],
            "vaults": [],
            "summary": {
                "totalAccountValue": None,
                "walletValue": None,
                "ordersValue": None,
                "vaultsValue": None,
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
    balance_block, balances, native_raw, stale = fetch_balances(wallet, [t["address"] for t in tokens])

    # funds resting on the book and funds deposited into vaults have left the
    # wallet balance but still belong to the user, so an account total built from
    # wallet balances alone understates it by exactly those two amounts
    locked = storage.open_order_locked_by_token(wallet)
    vaults_total, vault_rows = storage.vault_positions_usd(wallet)

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
                # exchange trade history is not indexed until the orderbook decode
                # lands, so a 24h change would be a guess. null renders as a dash
                "priceChange24h": None,
                "valueUsd": _fmt_usd(value) if value is not None else None,
                # wallet plus what this token has locked on the book, which is
                # what the row is really worth to the holder
                "totalValueUsd": _fmt_usd(combined) if combined is not None else None,
            }
        )
    rows.sort(key=lambda r: Decimal(r["totalValueUsd"] or r["valueUsd"] or 0), reverse=True)
    total = wallet_total + orders_total + vaults_total

    return {
        "wallet": wallet,
        "supported": True,
        "rows": rows,
        "vaults": [
            {**v, "valueUsd": _fmt_usd(v["valueUsd"]) if v["valueUsd"] is not None else None}
            for v in vault_rows
        ],
        "summary": {
            # the total is wallet plus book plus vaults; the parts are broken out
            # so the ui can show where the money actually sits
            "totalAccountValue": _fmt_usd(total),
            "walletValue": _fmt_usd(wallet_total),
            "ordersValue": _fmt_usd(orders_total),
            "vaultsValue": _fmt_usd(vaults_total),
            # these four light up with the orderbook serving layer; see DESIGN.md
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


# native mon balance for one wallet over one cached rpc read, for callers that do
# not need the full spot balance map
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


# one json-rpc batch: block number, native balance and every erc20 balance. cached
# per wallet so concurrent viewers share a read, stale served when the rpc fails
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
            # a transport failure serves the last snapshot and says so, rather than
            # rendering a portfolio of zeros that reads as real
            return cached[1], cached[2], cached[3], True
        raise
