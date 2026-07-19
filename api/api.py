from __future__ import annotations

import base64
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

import core.storage as storage
from api.x_api import router as x_router
from core import chain as h
from core.adapters.native import CURVE_SUPPLY as _NATIVE_CURVE_SUPPLY
from core.lifecycle import CurveState, resolve_phase
from core.storage import db_cursor

getcontext().prec = 100
# nad.fun sells 793,100,000 on its curve; ours sells 800,000,000
_NADFUN_CURVE_SUPPLY = 793_100_000 * 10**18


# derive phase and progress from stored fields so they cannot drift from the data
def _lifecycle_fields(*, source, circulating_supply, tx_count, migrated) -> dict[str, Any]:
    src = int(source or 0)
    curve_supply = _NATIVE_CURVE_SUPPLY if src == 0 else _NADFUN_CURVE_SUPPLY
    curve = CurveState(
        tokens_sold=int(circulating_supply or 0) * 10**18,
        curve_supply=curve_supply,
    )
    done = bool(migrated)
    phase = resolve_phase(
        curve=curve,
        has_trades=int(tx_count or 0) > 0,
        graduated=done and src == 0,
        migrated=done and src != 0,
    )
    return {"phase": phase.value, "progressBps": curve.progress_bps}


log = logging.getLogger("api")

if not log.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(formatter)
    log.addHandler(handler)

log.setLevel(logging.INFO)
log.propagate = False

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
LVMON = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
NATIVE_EQUIV_QUOTES = {WMON, LVMON}


# format a decimal as a trimmed plain string, 0 when empty or invalid
def _fmt(value) -> str:
    if value is None:
        return "0"
    try:
        d = Decimal(str(value)) if not isinstance(value, Decimal) else value
        d = d.quantize(Decimal("1e-18"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return "0"
    if abs(d) <= Decimal("1e-18"):
        return "0"
    if d == d.to_integral_value():
        return str(int(d))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


# format a usd decimal to 8dp as a trimmed plain string
def _fmt_usd(value) -> str:
    if value is None:
        return "0"
    try:
        d = Decimal(str(value)) if not isinstance(value, Decimal) else value
        d = d.quantize(Decimal("1e-8"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return "0"
    if abs(d) <= Decimal("1e-8"):
        return "0"
    if d == d.to_integral_value():
        return str(int(d))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


# shape one crystal pool row for the api
def _crystal_pool_row_to_api(row) -> dict[str, Any]:
    (
        market,
        quote_address,
        base_address,
        market_type,
        quote_decimals,
        base_decimals,
        quote_ticker,
        quote_name,
        base_ticker,
        base_name,
        taker_fee,
        is_amm_enabled,
        last_price,
        updated_at,
        created_at,
        reserve_quote,
        reserve_base,
        total_shares,
        tvl_usd,
        volume_24h_usd,
        fees_24h_usd,
        apy_24h,
        daily_yield_24h,
        last_sync_block,
        last_sync_at,
        *_rest,
    ) = row

    apy = Decimal(apy_24h or 0)
    daily_yield = Decimal(daily_yield_24h or 0)

    return {
        "market": (market or "").lower(),
        "address": (market or "").lower(),
        "quoteAsset": (quote_address or "").lower(),
        "baseAsset": (base_address or "").lower(),
        "quote": {
            "address": (quote_address or "").lower(),
            "decimals": int(quote_decimals or 0),
            "ticker": quote_ticker or "",
            "name": quote_name or "",
        },
        "base": {
            "address": (base_address or "").lower(),
            "decimals": int(base_decimals or 0),
            "ticker": base_ticker or "",
            "name": base_name or "",
        },
        "marketType": int(market_type or 0),
        "isAmmEnabled": bool(is_amm_enabled),
        "lastPrice": _fmt(last_price or Decimal(0)),
        "takerFee": str(int(taker_fee or 0)),
        "reserveQuote": str(int(reserve_quote or 0)),
        "reserveBase": str(int(reserve_base or 0)),
        "totalShares": str(int(total_shares or 0)),
        "tvlUsd": float(tvl_usd or 0.0),
        "volume24hUsd": float(volume_24h_usd or 0.0),
        "fees24hUsd": float(fees_24h_usd or 0.0),
        "apy24h": float(apy),
        "apy24hPercent": float(apy * Decimal(100)),
        "dailyYield24h": float(daily_yield),
        "dailyYieldPercent": float(daily_yield * Decimal(100)),
        "lastSyncBlock": int(last_sync_block or 0),
        "lastSyncAt": int(last_sync_at or 0),
        "updatedAt": int(last_sync_at or updated_at or created_at or 0),
        "createdAt": int(created_at or 0),
    }


AGGREGATOR_ADDR = "0x0B79d71AE99528D1dB24A4148b5f4F865cc2b137".lower()

_internal_addrs_cache: set[str] | None = None
_internal_addrs_ts: float = 0


# cached set of addresses excluded from holder counts
def _internal_addrs() -> set[str]:
    global _internal_addrs_cache, _internal_addrs_ts
    now = time.time()
    if _internal_addrs_cache is not None and (now - _internal_addrs_ts) < 60:
        return _internal_addrs_cache

    base: set[str] = {AGGREGATOR_ADDR}
    base.update(a.lower() for a in getattr(h, "ADDRS", []))

    for pool, _, _, _ in storage.load_all_pools():
        if pool:
            base.add(pool.lower())

    for addr in storage.load_holder_denylist():
        if addr:
            base.add(addr.lower())

    _internal_addrs_cache = base
    _internal_addrs_ts = now
    return base


_nadfun_v2_cache: set[str] | None = None
_nadfun_v2_ts: float = 0


# cached set of nadfun tokens on the v2 curve
def _nadfun_v2_set() -> set[str]:
    global _nadfun_v2_cache, _nadfun_v2_ts
    now = time.time()
    if _nadfun_v2_cache is not None and (now - _nadfun_v2_ts) < 60:
        return _nadfun_v2_cache
    s = {a.lower() for a in storage.load_nadfun_v2_tokens() if a}
    _nadfun_v2_cache = s
    _nadfun_v2_ts = now
    return s


# 1 or 2 for a nadfun token, 0 for any other source
def _nadfun_version(token: str, source) -> int:
    if int(source or 0) != 1:
        return 0
    return 2 if (token or "").lower() in _nadfun_v2_set() else 1


from collections import OrderedDict
from functools import wraps


# small in process lru cache with per entry expiry
class TTLCache:
    # start empty with a max entry count
    def __init__(self, max_size: int = 1000):
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size

    # value and hit flag, missing once expired
    def get(self, key: str) -> tuple[Any, bool]:
        if key not in self._cache:
            return None, False
        value, expires = self._cache[key]
        if time.time() > expires:
            del self._cache[key]
            return None, False
        self._cache.move_to_end(key)
        return value, True

    # store a value with a ttl, evicting the oldest when full
    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = (value, time.time() + ttl_seconds)

    # drop one key
    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    # drop every entry
    def clear(self) -> None:
        self._cache.clear()


_cache = TTLCache(max_size=2000)


# decorator caching an endpoint result under a prefixed key
def ttl_cache(prefix: str, ttl_seconds: int = 60):
    # wrap the endpoint with a cache lookup and store
    def decorator(func):
        @wraps(func)
        # look the key up, calling through and storing on a miss
        def wrapper(*args, **kwargs):
            key_parts = [str(a) for a in args]
            if kwargs:
                for k in sorted(kwargs.keys()):
                    key_parts.append(f"{k}={kwargs[k]}")
            cache_key = f"{prefix}:{':'.join(key_parts)}"
            value, hit = _cache.get(cache_key)
            if hit:
                return value
            result = func(*args, **kwargs)
            _cache.set(cache_key, result, ttl_seconds)
            return result

        return wrapper

    return decorator


# encode a pagination cursor as base64 json
def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# decode a base64 json pagination cursor
def _decode_cursor(cursor: str) -> dict[str, Any]:
    if cursor is None:
        raise HTTPException(400, "invalid cursor")
    try:
        s = str(cursor).strip()
        if not s:
            raise ValueError("empty cursor")
        pad = "=" * ((4 - (len(s) % 4)) % 4)
        data = base64.urlsafe_b64decode((s + pad).encode("ascii"))
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("cursor payload must be object")
        return obj
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "invalid cursor")


# read the sort key and token out of a positions cursor
def _parse_positions_cursor(cursor: str):
    obj = _decode_cursor(cursor)
    try:
        v_raw = obj.get("v")
        t_raw = obj.get("t")
        if v_raw is None or t_raw is None:
            raise ValueError("missing fields")
        return Decimal(str(v_raw)), str(t_raw).lower()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "invalid positions cursor")


# read the block and log index out of a history cursor
def _parse_history_cursor(cursor: str):
    obj = _decode_cursor(cursor)
    try:
        ts = int(obj.get("ts"))
        li = int(obj.get("li"))
        tx = str(obj.get("tx") or "").lower()
        if not tx:
            raise ValueError("missing tx")
        return ts, li, tx
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "invalid history cursor")


# holder counts and dev holdings for many tokens in one query
def _batch_get_holder_stats(token_addrs: list[str], excluded: set[str] | None = None) -> dict[str, dict]:
    if not token_addrs:
        return {}
    token_addrs = [(t or "").lower() for t in token_addrs if t]
    excluded_list = [a.lower() for a in (excluded or set()) if a]

    with db_cursor() as cur:
        cur.execute(
            """
            WITH req AS (
                SELECT token, LOWER(COALESCE(creator, '')) AS creator
                FROM launchpad_tokens
                WHERE token = ANY(%s)
            ),
            pos AS (
                SELECT LOWER(token) AS token, LOWER(user_address) AS user_address, balance_token
                FROM launchpad_positions
                WHERE token = ANY(%s)
            ),
            holder_counts AS (
                SELECT token, COUNT(*) AS cnt
                FROM pos
                WHERE balance_token > 1
                  AND (%s::text[] IS NULL OR NOT (user_address = ANY(%s)))
                GROUP BY token
            ),
            dev_holdings AS (
                SELECT p.token, p.balance_token AS dev_bal
                FROM pos p
                JOIN req r ON r.token = p.token
                WHERE p.balance_token > 0 AND p.user_address = r.creator
            ),
            ranked_top AS (
                SELECT
                    token,
                    user_address,
                    balance_token,
                    ROW_NUMBER() OVER (PARTITION BY token ORDER BY balance_token DESC, user_address ASC) AS rn
                FROM pos
                WHERE balance_token > 1
                  AND (%s::text[] IS NULL OR NOT (user_address = ANY(%s)))
            ),
            top10 AS (
                SELECT
                    token,
                    array_agg(user_address ORDER BY rn) AS top10_addrs,
                    SUM(balance_token) AS top10_sum
                FROM ranked_top
                WHERE rn <= 10
                GROUP BY token
            )
            SELECT
                r.token,
                COALESCE(h.cnt, 0) AS holder_count,
                COALESCE(d.dev_bal, 0) AS dev_holding,
                COALESCE(t.top10_addrs, ARRAY[]::text[]) AS top10_addresses,
                COALESCE(t.top10_sum, 0) AS top10_holding
            FROM req r
            LEFT JOIN holder_counts h ON h.token = r.token
            LEFT JOIN dev_holdings d ON d.token = r.token
            LEFT JOIN top10 t ON t.token = r.token
        """,
            (
                token_addrs,
                token_addrs,
                excluded_list if excluded_list else None,
                excluded_list if excluded_list else None,
                excluded_list if excluded_list else None,
                excluded_list if excluded_list else None,
            ),
        )
        rows = cur.fetchall()

    result = {}
    for row in rows:
        result[row[0]] = {
            "holder_count": int(row[1] or 0),
            "dev_holding": int(row[2] or 0),
            "top10_addresses": [a.lower() for a in (row[3] or [])],
            "top10_holding": int(row[4] or 0),
        }

    for token in token_addrs:
        if token not in result:
            result[token] = {"holder_count": 0, "dev_holding": 0, "top10_addresses": [], "top10_holding": 0}

    return result


# serialize many tokens for list endpoints in one round trip
def _batch_serialize_tokens(token_addrs: list[str], excluded: set[str]) -> dict[str, dict]:
    if not token_addrs:
        return {}

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                token, creator, name, symbol, metadata_cid, description,
                social1, social2, social3, social4, source,
                created_block, created_at, migrated, migrated_block,
                migrated_at, market, last_price_native, native_volume,
                token_volume, volume_usd, fees_usd, buy_count, sell_count,
                tx_count, circulating_supply, snipers_count, approaching_75,
                approaching_75_block, approaching_75_at,
                COALESCE(u.tokens_created, 0) as dev_tokens_created,
                COALESCE(u.tokens_graduated, 0) as dev_tokens_graduated,
                COALESCE(t.quote_token, '0x3bd359c1119da7da1d913d1c4d2b7c461115433a') as quote_token
            FROM launchpad_tokens t
            LEFT JOIN launchpad_users u ON u.address = t.creator
            WHERE token = ANY(%s)
        """,
            (token_addrs,),
        )
        rows = cur.fetchall()

    token_data = {}
    for row in rows:
        token = row[0]
        creator = (row[1] or "").lower()

        last_price_native = row[17] or Decimal(0)
        quote_token = (row[32] or WMON).lower()
        quote_price_usd = _quote_price_usd(quote_token)
        marketcap_native_raw = last_price_native * Decimal(1e9)
        marketcap_usd = marketcap_native_raw * quote_price_usd

        token_data[token] = {
            "token": token,
            "symbol": row[3],
            "name": row[2],
            "created_ts": row[12],
            "creator": creator,
            "metadata_cid": row[4],
            "source": int(row[10] or 0),
            "nadfunVersion": _nadfun_version(token, row[10]),
            "quote_token": quote_token,
            "quote_asset": quote_token,
            "native_volume": str(int(row[18] or 0)),
            "quote_volume": str(int(row[18] or 0)),
            "token_volume": str(int(row[19] or 0)),
            "volume_usd": _fmt_usd(row[20] or Decimal(0)),
            "fees_usd": _fmt_usd(row[21] or Decimal(0)),
            "marketcap_native_raw": _fmt(marketcap_native_raw),
            "marketcap_quote_raw": _fmt(marketcap_native_raw),
            "price_quote": _fmt(last_price_native),
            "marketcap_usd": _fmt_usd(marketcap_usd),
            "tx": {
                "buy": int(row[22] or 0),
                "sell": int(row[23] or 0),
                "total": int(row[24] or (int(row[22] or 0) + int(row[23] or 0))),
            },
            "migrated": bool(row[13]),
            "migrated_block": row[14],
            "migrated_at": row[15],
            "approaching_75": bool(row[27]),
            "approaching_75_block": row[28],
            "approaching_75_at": row[29],
            "social1": row[6],
            "social2": row[7],
            "social3": row[8],
            "social4": row[9],
            "market": row[16],
            "circulating_supply": str(int(row[25] or 0)),
            **_lifecycle_fields(
                source=int(row[10] or 0),
                circulating_supply=int(row[25] or 0),
                tx_count=int(row[24] or 0),
                migrated=bool(row[13]),
            ),
            "developer_tokens_created": int(row[30] or 0),
            "developer_tokens_graduated": int(row[31] or 0),
        }

    holder_stats = _batch_get_holder_stats(token_addrs, excluded=excluded)

    for token, data in token_data.items():
        stats = holder_stats.get(token, {})
        data["holders"] = stats.get("holder_count", 0)
        data["developer_holding"] = str(stats.get("dev_holding", 0))
        data["top10_holding"] = str(stats.get("top10_holding", 0))
        data["top10_addresses"] = stats.get("top10_addresses", [])

    for token, data in token_data.items():
        data["snipers"] = {"count": 0, "addresses": [], "holdingShare": 0.0}

    return token_data


# holder count, dev holding, top10 share and top10 addresses
def _holders_for_token(token_addr: str, creator: str | None) -> tuple[int, int, int, list[str]]:
    token_addr = token_addr.lower()
    creator_addr = (creator or "").lower()
    excluded = _internal_addrs()

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_address, balance_token
            FROM launchpad_positions
            WHERE token = %s AND balance_token > 1
            """,
            (token_addr,),
        )
        rows = cur.fetchall()

    dev_holding = 0
    filtered: list[tuple[int, str]] = []

    for ua, bal in rows:
        ua = ua.lower()
        bal = int(bal or 0)

        if ua == creator_addr:
            dev_holding = bal

        if ua not in excluded:
            filtered.append((bal, ua))

    filtered.sort(reverse=True)
    holder_count = len(filtered)
    top10 = filtered[:10]
    top10_holding = sum(b for b, _ in top10)
    top10_addresses = [addr for _, addr in top10]

    return holder_count, dev_holding, top10_holding, top10_addresses


# full api record for a single token
def _serialize_token(token_addr: str) -> dict[str, Any]:
    token_addr = token_addr.lower()

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                token,
                creator,
                name,
                symbol,
                metadata_cid,
                description,
                social1,
                social2,
                social3,
                social4,
                source,
                created_block,
                created_at,
                migrated,
                migrated_block,
                migrated_at,
                market,
                last_price_native,
                native_volume,
                token_volume,
                volume_usd,
                fees_usd,
                buy_count,
                sell_count,
                tx_count,
                circulating_supply,
                snipers_count,
                approaching_75,
                approaching_75_block,
                approaching_75_at,
                quote_token
            FROM launchpad_tokens
            WHERE token = %s
            """,
            (token_addr,),
        )
        row = cur.fetchone()

    if not row:
        return {}

    (
        token,
        creator,
        name,
        symbol,
        metadata_cid,
        description,
        social1,
        social2,
        social3,
        social4,
        source,
        created_block,
        created_at,
        migrated,
        migrated_block,
        migrated_at,
        market,
        last_price_native,
        native_volume,
        token_volume,
        volume_usd,
        fees_usd,
        buy_count,
        sell_count,
        tx_count,
        circulating_supply,
        snipers_count,
        approaching_75,
        approaching_75_block,
        approaching_75_at,
        quote_token,
    ) = row

    creator = creator or ""
    holders, dev_holding, top10_holding, top10_addresses = _holders_for_token(token, creator)

    last_price_native = last_price_native or Decimal(0)
    native_volume = int(native_volume or 0)
    token_volume = int(token_volume or 0)
    volume_usd = volume_usd or Decimal(0)
    fees_usd = fees_usd or Decimal(0)
    buy_count = int(buy_count or 0)
    sell_count = int(sell_count or 0)
    tx_count = int(tx_count or (buy_count + sell_count))
    circulating_supply = int(circulating_supply or 0)
    snipers_count = int(snipers_count or 0)

    marketcap_native_raw = last_price_native * Decimal(1e9)
    quote_token = (quote_token or WMON).lower()
    marketcap_usd = marketcap_native_raw * _quote_price_usd(quote_token)

    dev_tokens_created = 0
    dev_tokens_graduated = 0

    if creator:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT tokens_created, tokens_graduated
                FROM launchpad_users
                WHERE address = %s
                """,
                (creator.lower(),),
            )
            r = cur.fetchone()
        if r:
            dev_tokens_created = r[0] or 0
            dev_tokens_graduated = r[1] or 0

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_address
            FROM launchpad_snipers
            WHERE LOWER(token) = %s
            """,
            (token_addr,),
        )
        sniper_rows = cur.fetchall()

    sniper_addrs = [a[0].lower() for a in sniper_rows if a[0]]
    sniper_count = snipers_count if snipers_count else len(sniper_addrs)

    sniper_balance = 0
    if sniper_addrs:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(balance_token), 0)
                FROM launchpad_positions
                WHERE token = %s AND user_address = ANY(%s)
                """,
                (token_addr, sniper_addrs),
            )
            sb_row = cur.fetchone()
        sniper_balance = int(sb_row[0] or 0)

    sniper_share = float(Decimal(sniper_balance) / Decimal(1_000_000_000)) if sniper_balance > 0 else 0.0

    snipers_view = {
        "count": sniper_count,
        "addresses": sorted(sniper_addrs),
        "holdingShare": sniper_share,
    }

    return {
        "token": token,
        "symbol": symbol,
        "name": name,
        "created_ts": created_at,
        "creator": creator,
        "metadata_cid": metadata_cid,
        "source": int(source or 0),
        "nadfunVersion": _nadfun_version(token, source),
        "quote_token": quote_token,
        "quote_asset": quote_token,
        "holders": holders,
        "developer_holding": str(dev_holding),
        "top10_holding": str(top10_holding),
        "top10_addresses": top10_addresses,
        "native_volume": str(native_volume),
        "quote_volume": str(native_volume),
        "token_volume": str(token_volume),
        "volume_usd": _fmt_usd(volume_usd),
        "fees_usd": _fmt_usd(fees_usd),
        "marketcap_native_raw": _fmt(marketcap_native_raw),
        "marketcap_quote_raw": _fmt(marketcap_native_raw),
        "price_quote": _fmt(last_price_native),
        "marketcap_usd": _fmt_usd(marketcap_usd),
        "tx": {
            "buy": buy_count,
            "sell": sell_count,
            "total": tx_count,
        },
        "migrated": bool(migrated),
        "migrated_block": migrated_block,
        "migrated_at": migrated_at,
        "approaching_75": bool(approaching_75),
        "approaching_75_block": approaching_75_block,
        "approaching_75_at": approaching_75_at,
        "developer_tokens_created": dev_tokens_created,
        "developer_tokens_graduated": dev_tokens_graduated,
        "social1": social1,
        "social2": social2,
        "social3": social3,
        "social4": social4,
        "snipers": snipers_view,
        "market": market,
        "circulating_supply": str(int(circulating_supply or 0)),
        **_lifecycle_fields(
            source=source,
            circulating_supply=circulating_supply,
            tx_count=tx_count,
            migrated=migrated,
        ),
    }


_PRICE_SCALE = Decimal(10**9)
_PRICE_QUANTUM = Decimal(1).scaleb(-9)


# price scaled to marketcap units as a string, keeping 9 decimals
def _scaled_price(p: Any) -> str:
    scaled = (p or Decimal(0)) * _PRICE_SCALE
    return format(scaled.quantize(_PRICE_QUANTUM).normalize(), "f")


# read stored ohlcv bars for a token at one resolution
def _build_ohlcv_from_db(
    token_addr: str,
    bucket_seconds: int,
    max_buckets: int | None = None,
) -> list[dict[str, Any]]:
    if bucket_seconds <= 0:
        return []

    token_addr = token_addr.lower()

    limit_clause = ""
    params: list[Any] = [token_addr, bucket_seconds]

    if max_buckets is not None and max_buckets > 0:
        limit_clause = "ORDER BY bucket_start DESC LIMIT %s"
        params.append(max_buckets)
    else:
        limit_clause = "ORDER BY bucket_start DESC LIMIT 1000"

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT bucket_start, open_price, high_price, low_price, close_price, quote_volume
            FROM launchpad_ohlcv
            WHERE token = %s AND resolution_sec = %s
            {limit_clause}
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    rows.reverse()

    out: list[dict[str, Any]] = []
    for bucket_start, open_p, high_p, low_p, close_p, qv in rows:
        out.append(
            {
                "time": str(int(bucket_start)),
                "open": _scaled_price(open_p),
                "high": _scaled_price(high_p),
                "low": _scaled_price(low_p),
                "close": _scaled_price(close_p),
                "quoteVolume": str(int(qv or 0)),
            }
        )

    return out


app = FastAPI(title="backend", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(x_router)


_mon_price_cache: tuple[float, Decimal] | None = None


# latest mon usd price from storage
def _mon_price_usd() -> Decimal:
    global _mon_price_cache
    now = time.time()
    if _mon_price_cache and (now - _mon_price_cache[0]) < 10:
        return _mon_price_cache[1]
    try:
        px = storage.get_mon_price_usd()
        if px is None:
            result = Decimal("0.03")
        else:
            px_dec = Decimal(px)
            result = px_dec if px_dec > 0 else Decimal("0.03")
        _mon_price_cache = (now, result)
        return result
    except Exception:
        return Decimal("0.03")


# usd price for a quote token, mon for native equivalents
def _quote_price_usd(quote_token: str | None) -> Decimal:
    quote = (quote_token or WMON).lower()
    if quote in NATIVE_EQUIV_QUOTES:
        return _mon_price_usd()
    return Decimal(0)


# thin a series down to at most n points spread over time
def _sample_evenly_by_time(items, max_points: int, ts_getter) -> list:
    pts = [it for it in (items or []) if it is not None]
    if max_points <= 0 or len(pts) <= max_points:
        return pts
    pts = sorted(pts, key=lambda x: int(ts_getter(x) or 0))
    if len(pts) <= max_points:
        return pts
    start_ts = int(ts_getter(pts[0]) or 0)
    end_ts = int(ts_getter(pts[-1]) or 0)
    if end_ts <= start_ts:
        return pts[-max_points:]
    chosen: list[int] = []
    next_min_idx = 0
    span = end_ts - start_ts
    for i in range(max_points):
        target = start_ts + (span * i) / max(1, max_points - 1)
        best_idx = -1
        best_dist = float("inf")
        for j in range(next_min_idx, len(pts)):
            d = abs(int(ts_getter(pts[j]) or 0) - target)
            if d < best_dist:
                best_dist = d
                best_idx = j
            if int(ts_getter(pts[j]) or 0) > target and d > best_dist:
                break
        if best_idx < next_min_idx:
            best_idx = next_min_idx
        if best_idx < 0 or best_idx >= len(pts):
            break
        chosen.append(best_idx)
        next_min_idx = best_idx + 1
        if next_min_idx >= len(pts):
            break
    if not chosen:
        return pts[-max_points:]
    if chosen[-1] != len(pts) - 1:
        if len(chosen) < max_points:
            chosen.append(len(pts) - 1)
        else:
            chosen[-1] = len(pts) - 1
    out = []
    seen = set()
    for idx in sorted(chosen):
        if idx in seen:
            continue
        if 0 <= idx < len(pts):
            out.append(pts[idx])
            seen.add(idx)
    return out[:max_points] if out else pts[-max_points:]


from api.routes.launchpad import router as launchpad_router
from api.routes.markets import router as markets_router
from api.routes.pools import router as pools_router
from api.routes.system import router as system_router
from api.routes.vaults import router as vaults_router

app.include_router(launchpad_router)
app.include_router(system_router)
app.include_router(vaults_router)
app.include_router(markets_router)
app.include_router(pools_router)
