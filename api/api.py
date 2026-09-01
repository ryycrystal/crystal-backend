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
from core.adapters import nadfun as _nadfun_geo
from core.adapters.native import CURVE_SUPPLY as _NATIVE_CURVE_SUPPLY
from core.lifecycle import CurveState, resolve_phase
from core.storage import db_cursor

getcontext().prec = 100


def _api_source(source) -> int:
    return 1 if _nadfun_geo.is_nadfun_source(source) else int(source or 0)


def _curve_supply_for(source) -> int:
    if _nadfun_geo.is_nadfun_source(source):
        return _nadfun_geo.curve_supply_for(source)
    return _NATIVE_CURVE_SUPPLY


def _lifecycle_fields(*, source, circulating_supply, tx_count, migrated) -> dict[str, Any]:
    src = int(source or 0)
    curve_supply = _curve_supply_for(src)
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
    bps = curve.progress_bps
    if done:
        bps = 10000
    return {"phase": phase.value, "progressBps": bps}


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
        "verified": True,
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


def _static_internal_addrs() -> set[str]:
    base: set[str] = {AGGREGATOR_ADDR}
    base.update(a.lower() for a in getattr(h, "ADDRS", []))
    return base


def _sql_not_internal(col: str) -> str:
    return (
        f"NOT EXISTS (SELECT 1 FROM launchpad_pools _ex_p WHERE _ex_p.pool = {col})"
        f" AND NOT EXISTS (SELECT 1 FROM holder_denylist _ex_d WHERE _ex_d.address = {col})"
    )


_nadfun_v2_cache: set[str] | None = None
_nadfun_v2_ts: float = 0


def _nadfun_v2_set() -> set[str]:
    global _nadfun_v2_cache, _nadfun_v2_ts
    now = time.time()
    if _nadfun_v2_cache is not None and (now - _nadfun_v2_ts) < 60:
        return _nadfun_v2_cache
    s = {a.lower() for a in storage.load_nadfun_v2_tokens() if a}
    _nadfun_v2_cache = s
    _nadfun_v2_ts = now
    return s


def _nadfun_version(token: str, source) -> int:
    src = int(source or 0)
    if src == 1:
        return 2 if (token or "").lower() in _nadfun_v2_set() else 1
    if _nadfun_geo.is_nadfun_source(src):
        return _nadfun_geo.version_of(src)
    return 0


import threading
from collections import OrderedDict
from functools import wraps


class TTLCache:
    def __init__(self, max_size: int = 1000):
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> tuple[Any, bool]:
        if key not in self._cache:
            return None, False
        value, expires = self._cache[key]
        if time.time() > expires:
            del self._cache[key]
            return None, False
        self._cache.move_to_end(key)
        return value, True

    def get_stale(self, key: str) -> tuple[Any, bool, float]:
        if key not in self._cache:
            return None, False, 0.0
        value, expires = self._cache[key]
        self._cache.move_to_end(key)
        return value, time.time() <= expires, expires

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()


_cache = TTLCache(max_size=2000)


_refresh_inflight: set[str] = set()
_refresh_guard = threading.Lock()


def _spawn_refresh(cache_key, func, args, kwargs, ttl_seconds: float) -> None:
    with _refresh_guard:
        if cache_key in _refresh_inflight:
            return
        _refresh_inflight.add(cache_key)

    def run():
        try:
            _cache.set(cache_key, func(*args, **kwargs), ttl_seconds)
        except Exception as e:
            log.warning("cache refresh failed for %s: %r", cache_key, e)
        finally:
            with _refresh_guard:
                _refresh_inflight.discard(cache_key)

    threading.Thread(target=run, daemon=True).start()


def ttl_cache(prefix: str, ttl_seconds: float = 60, serve_stale_seconds: float = 0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            key_parts = [str(a) for a in args]
            if kwargs:
                for k in sorted(kwargs.keys()):
                    key_parts.append(f"{k}={kwargs[k]}")
            cache_key = f"{prefix}:{':'.join(key_parts)}"
            if serve_stale_seconds > 0:
                value, fresh, expires = _cache.get_stale(cache_key)
                if fresh:
                    return value
                if expires > 0 and (time.time() - expires) <= serve_stale_seconds:
                    _spawn_refresh(cache_key, func, args, kwargs, ttl_seconds)
                    return value
            else:
                value, hit = _cache.get(cache_key)
                if hit:
                    return value
            result = func(*args, **kwargs)
            _cache.set(cache_key, result, ttl_seconds)
            return result

        return wrapper

    return decorator


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


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


def _batch_get_holder_stats(token_addrs: list[str], excluded: set[str] | None = None) -> dict[str, dict]:
    if not token_addrs:
        return {}
    token_addrs = [(t or "").lower() for t in token_addrs if t]
    excluded_list = [a.lower() for a in (excluded or set()) if a]
    not_internal = _sql_not_internal("p.user_address")

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                r.token,
                COALESCE(hc.cnt, 0) AS holder_count,
                COALESCE(dev.bal, 0) AS dev_holding,
                COALESCE(t10.addrs, ARRAY[]::text[]) AS top10_addresses,
                COALESCE(t10.total, 0) AS top10_holding,
                COALESCE(sn.cnt, 0) AS sniper_count,
                COALESCE(sn.addrs, ARRAY[]::text[]) AS sniper_addresses,
                COALESCE(sn.total, 0) AS sniper_holding,
                COALESCE(ins.total, 0) AS insider_holding,
                COALESCE(pt.cnt, 0) AS pro_traders
            FROM (
                SELECT token, LOWER(COALESCE(creator, '')) AS creator
                FROM launchpad_tokens
                WHERE token = ANY(%s)
            ) r
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS cnt
                FROM launchpad_positions p
                WHERE p.token = r.token AND p.balance_token > 1
                  AND (%s::text[] IS NULL OR NOT (p.user_address = ANY(%s)))
                  AND {not_internal}
            ) hc ON TRUE
            LEFT JOIN LATERAL (
                SELECT p.balance_token AS bal
                FROM launchpad_positions p
                WHERE p.token = r.token AND p.user_address = r.creator AND p.balance_token > 0
            ) dev ON TRUE
            LEFT JOIN LATERAL (
                SELECT array_agg(x.user_address ORDER BY x.balance_token DESC, x.user_address ASC) AS addrs,
                       SUM(x.balance_token) AS total
                FROM (
                    SELECT p.user_address, p.balance_token
                    FROM launchpad_positions p
                    WHERE p.token = r.token AND p.balance_token > 1
                      AND (%s::text[] IS NULL OR NOT (p.user_address = ANY(%s)))
                      AND {not_internal}
                    ORDER BY p.balance_token DESC, p.user_address ASC
                    LIMIT 10
                ) x
            ) t10 ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS cnt,
                       array_agg(p.user_address) AS addrs,
                       SUM(p.balance_token) AS total
                FROM launchpad_snipers s
                JOIN launchpad_positions p ON p.token = r.token AND p.user_address = s.user_address
                WHERE s.token = r.token
            ) sn ON TRUE
            LEFT JOIN LATERAL (
                SELECT SUM(p.balance_token) AS total
                FROM launchpad_positions p
                WHERE p.token = r.token AND p.balance_token > (p.token_bought - p.token_sold) + 1e18
            ) ins ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS cnt
                FROM launchpad_positions p
                WHERE p.token = r.token AND p.realized_pnl_native > 0 AND p.trade_count >= 10
            ) pt ON TRUE
        """,
            (
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
            "sniper_count": int(row[5] or 0),
            "sniper_addresses": sorted(a.lower() for a in (row[6] or [])),
            "sniper_holding": int(row[7] or 0),
            "insider_holding": int(row[8] or 0),
            "pro_traders": int(row[9] or 0),
        }

    for token in token_addrs:
        if token not in result:
            result[token] = {
                "holder_count": 0,
                "dev_holding": 0,
                "top10_addresses": [],
                "top10_holding": 0,
                "sniper_count": 0,
                "sniper_addresses": [],
                "sniper_holding": 0,
                "insider_holding": 0,
                "pro_traders": 0,
            }

    return result


def _batch_get_price_changes(token_addrs: list[str]) -> dict[str, dict[str, str]]:
    if not token_addrs:
        return {}
    cutoff = int(time.time()) - 86400
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.token, t.last_price_native, r.price_native, f.price_native
            FROM launchpad_tokens t
            LEFT JOIN LATERAL (
                SELECT price_native
                FROM launchpad_trades
                WHERE token = t.token AND timestamp <= %s
                ORDER BY timestamp DESC, log_index DESC
                LIMIT 1
            ) r ON TRUE
            LEFT JOIN LATERAL (
                -- the very first recorded trade, the launch reference. observed data,
                -- so it never depends on v0, which is settable and changes at go-live
                SELECT price_native
                FROM launchpad_trades
                WHERE token = t.token
                ORDER BY timestamp ASC, log_index ASC
                LIMIT 1
            ) f ON TRUE
            WHERE t.token = ANY(%s)
            """,
            (cutoff, token_addrs),
        )
        rows = cur.fetchall()

    def pct(last_d: Decimal, ref) -> str | None:
        ref_d = Decimal(ref or 0)
        if ref_d <= 0 or last_d <= 0:
            return None
        return _fmt((last_d - ref_d) / ref_d * Decimal(100))

    out: dict[str, dict[str, str]] = {}
    for token, last, ref_24h, ref_launch in rows:
        last_d = Decimal(last or 0)
        out[token] = {
            "change_pct_24h": pct(last_d, ref_24h),
            "change_pct_since_launch": pct(last_d, ref_launch),
        }
    return out


def _apply_live_pool_reserves(token_data: dict[str, dict]) -> None:
    migrated = [t for t, d in token_data.items() if d.get("migrated")]
    if not migrated:
        return

    try:
        pair_reserves = storage.pool_reserves_for_tokens(migrated)
    except Exception as e:
        log.warning("pool reserve lookup failed: %r", e)
        pair_reserves = {}

    for token, res in pair_reserves.items():
        data = token_data.get(token)
        if data is None:
            continue
        data["reserveQuote"] = res["reserveNative"]
        data["reserveBase"] = res["reserveToken"]
        data["reservesFrom"] = "pair"
        data["reservesSyncedAt"] = res.get("syncedAt", 0)

    remaining = {
        (token_data[t].get("market") or "").lower(): t
        for t in migrated
        if t not in pair_reserves and token_data[t].get("market")
    }
    if not remaining:
        return
    try:
        market_reserves = storage.crystal_pool_reserves_for_markets(list(remaining))
    except Exception as e:
        log.warning("crystal pool reserve lookup failed: %r", e)
        return

    for market, res in market_reserves.items():
        token = remaining.get(market)
        if not token:
            continue
        data = token_data[token]
        data["reserveQuote"] = res["reserveQuote"]
        data["reserveBase"] = res["reserveBase"]
        data["reservesFrom"] = "crystal_pool"
        data["reservesSyncedAt"] = res.get("syncedAt", 0)


def _batch_serialize_tokens(token_addrs: list[str]) -> dict[str, dict]:
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
                COALESCE(t.quote_token, '0x3bd359c1119da7da1d913d1c4d2b7c461115433a') as quote_token,
                t.curve_native_reserve, t.curve_token_reserve
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
            "imageUrl": row[4],
            "source": _api_source(row[10]),
            "sourceRaw": int(row[10] or 0),
            "nadfunVersion": _nadfun_version(token, row[10]),
            "quote_token": quote_token,
            "native_volume": str(int(row[18] or 0)),
            "token_volume": str(int(row[19] or 0)),
            "volume_usd": _fmt_usd(row[20] or Decimal(0)),
            "fees_usd": _fmt_usd(row[21] or Decimal(0)),
            "marketcap_native_raw": _fmt(marketcap_native_raw),
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
            "reserveQuote": str(int(row[33] or 0)),
            "reserveBase": str(int(row[34] or 0)),
            "reservesFrom": "curve",
            "reservesSyncedAt": 0,
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

    _apply_live_pool_reserves(token_data)

    holder_stats = _batch_get_holder_stats(token_addrs, excluded=_static_internal_addrs())

    for token, data in token_data.items():
        stats = holder_stats.get(token, {})
        data["holders"] = stats.get("holder_count", 0)
        data["developer_holding"] = str(stats.get("dev_holding", 0))
        data["top10_holding"] = str(stats.get("top10_holding", 0))
        data["top10_addresses"] = stats.get("top10_addresses", [])

    changes = _batch_get_price_changes(list(token_data.keys()))
    raw_sources = {t: d.get("sourceRaw") for t, d in token_data.items()}
    _markets = [(d.get("market") or "").lower() for d in token_data.values() if d.get("market")]
    pair_fee_map = storage.get_pair_fees_batch(_markets)
    taker_fee_map = storage.get_taker_fees_batch(_markets)

    for token, data in token_data.items():
        stats = holder_stats.get(token, {})
        sniper_bal = int(stats.get("sniper_holding", 0))
        data["snipers"] = {
            "count": int(stats.get("sniper_count", 0)),
            "addresses": stats.get("sniper_addresses", []),
            "holdingShare": float(Decimal(sniper_bal) / _PCT_OF_SUPPLY) if sniper_bal > 0 else 0.0,
        }
        data["insider_holding"] = str(int(stats.get("insider_holding", 0)))
        data["pro_traders"] = int(stats.get("pro_traders", 0))
        ch = changes.get(token) or {}
        data["change_pct_24h"] = ch.get("change_pct_24h")
        data["change_pct_since_launch"] = ch.get("change_pct_since_launch")
        market = (data.get("market") or "").lower()
        src = int(raw_sources.get(token) or 0)
        data["fees"] = {
            "curveFeeRate": _fmt(_nadfun_geo.fee_rate_for(src)) if _nadfun_geo.is_nadfun_source(src) else None,
            "pair": pair_fee_map.get(market) if src != 0 else None,
            "crystalMarket": (
                {"market": market, "takerFee": taker_fee_map[market]} if src == 0 and market in taker_fee_map else None
            ),
        }

    return token_data


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

    sniper_share = float(Decimal(sniper_balance) / _PCT_OF_SUPPLY) if sniper_balance > 0 else 0.0

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
        "imageUrl": metadata_cid,
        "source": _api_source(source),
        "nadfunVersion": _nadfun_version(token, source),
        "quote_token": quote_token,
        "holders": holders,
        "developer_holding": str(dev_holding),
        "top10_holding": str(top10_holding),
        "top10_addresses": top10_addresses,
        "native_volume": str(native_volume),
        "token_volume": str(token_volume),
        "volume_usd": _fmt_usd(volume_usd),
        "fees_usd": _fmt_usd(fees_usd),
        "marketcap_native_raw": _fmt(marketcap_native_raw),
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

_WEI = Decimal(10) ** 18

_PCT_OF_SUPPLY = Decimal(10) ** 25


def _scaled_price(p: Any) -> str:
    scaled = (p or Decimal(0)) * _PRICE_SCALE
    return format(scaled.quantize(_PRICE_QUANTUM).normalize(), "f")


def _initial_price_kline(
    created_at: Any,
    price_native: Any,
    bucket_seconds: int,
    before_ts: int | None = None,
) -> dict[str, Any] | None:
    """Return a response-only seed point until the first real OHLC bucket exists."""
    try:
        timestamp = int(created_at or 0)
        price = Decimal(price_native or 0)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if bucket_seconds <= 0 or timestamp <= 0 or price <= 0:
        return None

    bucket_start = (timestamp // bucket_seconds) * bucket_seconds
    if before_ts is not None and before_ts > 0 and bucket_start >= before_ts:
        return None

    scaled = _scaled_price(price)
    return {
        "time": str(bucket_start),
        "open": scaled,
        "high": scaled,
        "low": scaled,
        "close": scaled,
        "quoteVolume": "0",
    }


def _build_ohlcv_from_db(
    token_addr: str,
    bucket_seconds: int,
    max_buckets: int | None = None,
    before_ts: int | None = None,
) -> list[dict[str, Any]]:
    if bucket_seconds <= 0:
        return []

    token_addr = token_addr.lower()

    where = "token = %s AND resolution_sec = %s"
    params: list[Any] = [token_addr, bucket_seconds]

    if before_ts is not None and before_ts > 0:
        where += " AND bucket_start < %s"
        params.append(int(before_ts))

    limit = max_buckets if (max_buckets is not None and max_buckets > 0) else 1000
    params.append(int(limit) + 1)

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT bucket_start, open_price, high_price, low_price, close_price, quote_volume
            FROM launchpad_ohlcv
            WHERE {where}
            ORDER BY bucket_start DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    rows.reverse()
    seeded = len(rows) > limit
    seed_rows = rows[:1] if seeded else []
    rows = rows[1:] if seeded else rows

    out: list[dict[str, Any]] = []
    prev_close: Decimal | None = None
    if seed_rows:
        prev_close = Decimal(seed_rows[0][4] or 0)
    for bucket_start, open_p, high_p, low_p, close_p, qv in rows:
        o = Decimal(open_p or 0)
        hi = Decimal(high_p or 0)
        lo = Decimal(low_p or 0)
        cl = Decimal(close_p or 0)
        if prev_close is not None and prev_close > 0:
            o = prev_close
            hi = max(hi, o)
            lo = min(lo, o) if lo > 0 else o
        out.append(
            {
                "time": str(int(bucket_start)),
                "open": _scaled_price(o),
                "high": _scaled_price(hi),
                "low": _scaled_price(lo),
                "close": _scaled_price(cl),
                "quoteVolume": str(int(qv or 0)),
            }
        )
        if cl > 0:
            prev_close = cl

    return out


app = FastAPI(title="backend", version="0.1.0")

_EDGE_CACHEABLE = (
    "/tokens",
    "/token/",
    "/chart/",
    "/stats/",
    "/holders/",
    "/mon-usd/",
    "/search/",
    "/pair/",
    "/markets/",
    "/pools/list",
    "/tiers",
    "/leaderboard",
)


@app.middleware("http")
async def _cache_control(request, call_next):
    response = await call_next(request)
    if request.method == "GET" and "cache-control" not in response.headers:
        path = request.url.path
        if any(path.startswith(p) for p in _EDGE_CACHEABLE):
            response.headers["Cache-Control"] = "public, s-maxage=1, stale-while-revalidate=2"
        else:
            response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(x_router)


_mon_price_cache: tuple[float, Decimal] | None = None


_lvmon_rate_cache: tuple[float, Decimal] | None = None


def _lvmon_rate() -> Decimal:
    global _lvmon_rate_cache
    now = time.time()
    if _lvmon_rate_cache and (now - _lvmon_rate_cache[0]) < 30:
        return _lvmon_rate_cache[1]
    try:
        stored = storage.get_lvmon_rate()
        rate = Decimal(stored) if stored is not None else Decimal(1)
        if rate <= 0:
            rate = Decimal(1)
    except Exception:
        return Decimal(1)
    _lvmon_rate_cache = (now, rate)
    return rate


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


def _quote_price_usd(quote_token: str | None) -> Decimal:
    quote = (quote_token or WMON).lower()
    if quote == LVMON:
        return _mon_price_usd() * _lvmon_rate()
    if quote in NATIVE_EQUIV_QUOTES:
        return _mon_price_usd()
    return Decimal(0)


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
from api.routes.orderbook import router as orderbook_router
from api.routes.pools import router as pools_router
from api.routes.referrals import router as referrals_router
from api.routes.system import router as system_router
from api.routes.trackers import router as trackers_router
from api.routes.vaults import router as vaults_router

app.include_router(launchpad_router)
app.include_router(referrals_router)
app.include_router(system_router)
app.include_router(trackers_router)
app.include_router(vaults_router)
app.include_router(markets_router)
app.include_router(pools_router)
app.include_router(orderbook_router)

from api.ws import router as ws_router  # noqa: E402
from api.x_track import router as x_track_router  # noqa: E402

app.include_router(ws_router)
app.include_router(x_track_router)
