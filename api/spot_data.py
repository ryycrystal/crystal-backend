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

from core.storage import db_cursor

_BALANCE_TTL_SECONDS = 3.0
_BALANCEOF_SELECTOR = "0x70a08231"

# wallet -> (fetched_at, block, {token -> raw balance}, native raw)
_balance_cache: dict[str, tuple[float, int, dict[str, int], int]] = {}

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
