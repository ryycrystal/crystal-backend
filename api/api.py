from __future__ import annotations
from decimal import Decimal, getcontext, ROUND_HALF_UP, InvalidOperation
from typing import Dict, Any, List, Tuple
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import time
import logging
import traceback
import core.storage as storage
from core import chain as h
from api.x_api import router as x_router
from core.storage import db_cursor

getcontext().prec = 100

log = logging.getLogger("api")

if not log.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handler.setFormatter(formatter)
    log.addHandler(handler)

log.setLevel(logging.INFO)
log.propagate = False

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
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
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
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s if s else "0"


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

    _internal_addrs_cache = base
    _internal_addrs_ts = now
    return base


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

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
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


def ttl_cache(prefix: str, ttl_seconds: int = 60):
    def decorator(func):
        @wraps(func)
        def wrapper(*args):
            cache_key = f"{prefix}:{':'.join(str(a) for a in args)}"
            value, hit = _cache.get(cache_key)
            if hit:
                return value
            result = func(*args)
            _cache.set(cache_key, result, ttl_seconds)
            return result
        return wrapper
    return decorator


def _batch_get_holder_stats(token_addrs: list[str], excluded: set[str]) -> dict[str, dict]:
    if not token_addrs:
        return {}

    excluded_list = list(excluded)
    print(f"[PERF]     holder_stats: {len(token_addrs)} tokens, {len(excluded_list)} excluded addrs", flush=True)
    _ht0 = time.time()

    with db_cursor() as cur:
        cur.execute("""
            SELECT
                t.token,
                t.creator,
                COALESCE(hc.cnt, 0) as holder_count,
                COALESCE(dh.dev_bal, 0) as dev_holding,
                COALESCE(t10.top10_addrs, ARRAY[]::text[]) as top10_addresses,
                COALESCE(t10.top10_sum, 0) as top10_holding
            FROM launchpad_tokens t
            LEFT JOIN LATERAL (
                SELECT COUNT(*) as cnt
                FROM launchpad_positions p
                WHERE p.token = t.token AND p.balance_token > 1 AND p.user_address <> ALL(%s)
            ) hc ON true
            LEFT JOIN LATERAL (
                SELECT p.balance_token as dev_bal
                FROM launchpad_positions p
                WHERE p.token = t.token AND LOWER(p.user_address) = LOWER(t.creator) AND p.balance_token > 0
                LIMIT 1
            ) dh ON true
            LEFT JOIN LATERAL (
                SELECT
                    array_agg(sub.user_address) as top10_addrs,
                    SUM(sub.balance_token) as top10_sum
                FROM (
                    SELECT user_address, balance_token
                    FROM launchpad_positions p
                    WHERE p.token = t.token AND p.balance_token > 1 AND p.user_address <> ALL(%s)
                    ORDER BY p.balance_token DESC
                    LIMIT 10
                ) sub
            ) t10 ON true
            WHERE t.token = ANY(%s)
        """, (excluded_list, excluded_list, token_addrs))
        rows = cur.fetchall()
    _ht1 = time.time()
    print(f"[PERF]     holder_stats SQL: {(_ht1-_ht0)*1000:.1f}ms, {len(rows)} rows", flush=True)

    result = {}
    for row in rows:
        result[row[0]] = {
            "holder_count": int(row[2] or 0),
            "dev_holding": int(row[3] or 0),
            "top10_addresses": [a.lower() for a in (row[4] or [])],
            "top10_holding": int(row[5] or 0),
        }

    for token in token_addrs:
        if token not in result:
            result[token] = {"holder_count": 0, "dev_holding": 0, "top10_addresses": [], "top10_holding": 0}

    return result


def _batch_serialize_tokens(token_addrs: list[str], excluded: set[str]) -> dict[str, dict]:
    if not token_addrs:
        return {}

    _t0 = time.time()
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                token, creator, name, symbol, metadata_cid, description,
                social1, social2, social3, social4, source,
                created_block, created_at, migrated, migrated_block,
                migrated_at, market, last_price_native, native_volume,
                token_volume, volume_usd, fees_usd, buy_count, sell_count,
                tx_count, circulating_supply, snipers_count, approaching_75,
                approaching_75_block, approaching_75_at
            FROM launchpad_tokens
            WHERE token = ANY(%s)
        """, (token_addrs,))
        rows = cur.fetchall()
    _t1 = time.time()
    print(f"[PERF]   token metadata query: {(_t1-_t0)*1000:.1f}ms", flush=True)

    mon_price = _mon_price_usd()
    token_data = {}
    creators = {}
    for row in rows:
        token = row[0]
        creator = row[1] or ""
        creators[token] = creator

        last_price_native = row[17] or Decimal(0)
        marketcap_native_raw = last_price_native * Decimal(1e9)
        marketcap_usd = marketcap_native_raw * mon_price

        token_data[token] = {
            "token": token,
            "symbol": row[3],
            "name": row[2],
            "created_ts": row[12],
            "creator": creator,
            "metadata_cid": row[4],
            "source": int(row[10] or 0),
            "native_volume": str(int(row[18] or 0)),
            "token_volume": str(int(row[19] or 0)),
            "volume_usd": _fmt_usd(row[20] or Decimal(0)),
            "fees_usd": _fmt_usd(row[21] or Decimal(0)),
            "marketcap_native_raw": _fmt(marketcap_native_raw),
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
        }

    _t2 = time.time()
    holder_stats = _batch_get_holder_stats(token_addrs, excluded)
    _t3 = time.time()
    print(f"[PERF]   holder stats query: {(_t3-_t2)*1000:.1f}ms", flush=True)

    for token, data in token_data.items():
        stats = holder_stats.get(token, {})
        data["holders"] = stats.get("holder_count", 0)
        data["developer_holding"] = str(stats.get("dev_holding", 0))
        data["top10_holding"] = str(stats.get("top10_holding", 0))
        data["top10_addresses"] = stats.get("top10_addresses", [])

    creator_addrs = list(set(c for c in creators.values() if c))
    creator_stats = {}
    if creator_addrs:
        with db_cursor() as cur:
            cur.execute("""
                SELECT address, tokens_created, tokens_graduated
                FROM launchpad_users
                WHERE address = ANY(%s)
            """, (creator_addrs,))
            for addr, tc, tg in cur.fetchall():
                creator_stats[addr.lower()] = {"created": tc or 0, "graduated": tg or 0}
    _t4 = time.time()
    print(f"[PERF]   creator stats query: {(_t4-_t3)*1000:.1f}ms", flush=True)

    for token, data in token_data.items():
        creator = creators.get(token, "").lower()
        cs = creator_stats.get(creator, {"created": 0, "graduated": 0})
        data["developer_tokens_created"] = cs["created"]
        data["developer_tokens_graduated"] = cs["graduated"]
        data["snipers"] = {"count": 0, "addresses": [], "holdingShare": 0.0}

    return token_data

def _holders_for_token(token_addr: str, creator: str | None) -> Tuple[int, int, int, List[str]]:
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
    filtered: List[Tuple[int, str]] = []

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


def _serialize_token(token_addr: str) -> Dict[str, Any]:
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
                approaching_75_at
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
    marketcap_usd = marketcap_native_raw * _mon_price_usd()

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
        "holders": holders,
        "developer_holding": str(dev_holding),
        "top10_holding": str(top10_holding),
        "top10_addresses": top10_addresses,
        "native_volume": str(native_volume),
        "token_volume": str(token_volume),
        "volume_usd": _fmt_usd(volume_usd),
        "fees_usd": _fmt_usd(fees_usd),
        "marketcap_native_raw": _fmt(marketcap_native_raw),
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
    }


def _build_ohlcv_from_db(
    token_addr: str,
    bucket_seconds: int,
    max_buckets: int | None = None,
) -> List[Dict[str, Any]]:
    if bucket_seconds <= 0:
        return []

    token_addr = token_addr.lower()

    limit_clause = ""
    params: List[Any] = [token_addr, bucket_seconds]

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

    out: List[Dict[str, Any]] = []
    for bucket_start, open_p, high_p, low_p, close_p, qv in rows:
        open_wad = (open_p or Decimal(0)) * Decimal(1e9)
        high_wad = (high_p or Decimal(0)) * Decimal(1e9)
        low_wad = (low_p or Decimal(0)) * Decimal(1e9)
        close_wad = (close_p or Decimal(0)) * Decimal(1e9)
        quote_volume = int(qv or 0)

        out.append(
            {
                "time": str(int(bucket_start)),
                "open": str(int(open_wad)),
                "high": str(int(high_wad)),
                "low": str(int(low_wad)),
                "close": str(int(close_wad)),
                "quoteVolume": str(quote_volume),
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


@app.get("/health")
def health() -> Dict[str, Any]:
    log.info("health endpoint hit")
    return {"ok": True}


@app.get("/tokens")
def list_tokens() -> Dict[str, List[Dict[str, Any]]]:
    t0 = time.time()
    excluded = _internal_addrs()
    t1 = time.time()
    print(f"[PERF] _internal_addrs: {(t1-t0)*1000:.1f}ms", flush=True)

    with db_cursor() as cur:
        cur.execute("""
            SELECT token, circulating_supply
            FROM launchpad_tokens
            WHERE migrated = TRUE
            ORDER BY migrated_at DESC NULLS LAST, migrated_block DESC NULLS LAST
            LIMIT 30
        """)
        grad_rows = cur.fetchall()

    graduated_ids = {t.lower() for (t, _) in grad_rows}

    with db_cursor() as cur:
        if graduated_ids:
            cur.execute("""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                AND migrated = FALSE
                AND token <> ALL(%s)
                ORDER BY (circulating_supply::numeric / 793100000) DESC
                LIMIT 30
            """, (list(graduated_ids),))
        else:
            cur.execute("""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                AND migrated = FALSE
                ORDER BY (circulating_supply::numeric / 793100000) DESC
                LIMIT 30
            """)
        appr_rows = cur.fetchall()

    approaching_ids = {t.lower() for (t, _) in appr_rows}
    excluded_ids = graduated_ids | approaching_ids

    with db_cursor() as cur:
        if excluded_ids:
            cur.execute("""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE token <> ALL(%s)
                ORDER BY created_at DESC NULLS LAST, created_block DESC NULLS LAST
                LIMIT 30
            """, (list(excluded_ids),))
        else:
            cur.execute("""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                ORDER BY created_at DESC NULLS LAST, created_block DESC NULLS LAST
                LIMIT 30
            """)
        created_rows = cur.fetchall()

    all_token_addrs = [t for t, _ in grad_rows + appr_rows + created_rows]
    circ_map = {t: c for t, c in grad_rows + appr_rows + created_rows}
    t2 = time.time()
    print(f"[PERF] fetch token lists: {(t2-t1)*1000:.1f}ms, {len(all_token_addrs)} tokens", flush=True)
    token_data = _batch_serialize_tokens(all_token_addrs, excluded)
    t3 = time.time()
    print(f"[PERF] _batch_serialize_tokens: {(t3-t2)*1000:.1f}ms", flush=True)

    def with_graduation_pct(token_addr):
        data = token_data.get(token_addr, {})
        if data:
            data["graduationPercentageBps"] = (circ_map.get(token_addr) or 0) / 793100000
        return data

    recent_graduated_out = [with_graduation_pct(t) for t, _ in grad_rows if t in token_data]
    recent_approaching_out = [with_graduation_pct(t) for t, _ in appr_rows if t in token_data]
    recent_created_out = [with_graduation_pct(t) for t, _ in created_rows if t in token_data]

    result = {
        "recent_created": recent_created_out,
        "recent_approaching": recent_approaching_out,
        "recent_graduated": recent_graduated_out,
    }

    dt = (time.time() - t0) * 1000
    log.info("token_list dt_ms=%.1f", dt)

    return result


def _get_token_core_stats(token_addr: str, day_ago: int, excluded: set[str]) -> dict | None:
    excluded_list = list(excluded)
    with db_cursor() as cur:
        cur.execute("""
            WITH token_data AS (
                SELECT * FROM launchpad_tokens WHERE token = %s
            ),
            holder_stats AS (
                SELECT
                    COUNT(*) FILTER (WHERE user_address <> ALL(%s)) as holder_count
                FROM launchpad_positions
                WHERE token = %s AND balance_token > 1
            ),
            trade_stats_24h AS (
                SELECT
                    COALESCE(SUM(native_amount), 0) as volume_native_24h,
                    COALESCE(SUM(usd_amount), 0) as volume_usd_24h,
                    COUNT(*) FILTER (WHERE is_buy) as buys_24h,
                    COUNT(*) FILTER (WHERE NOT is_buy) as sells_24h
                FROM launchpad_trades
                WHERE token = %s AND timestamp >= %s
            ),
            buyer_seller_counts AS (
                SELECT
                    COUNT(*) FILTER (WHERE buy_count > 0 AND user_address <> ALL(%s)) as distinct_buyers,
                    COUNT(*) FILTER (WHERE sell_count > 0 AND user_address <> ALL(%s)) as distinct_sellers
                FROM launchpad_positions
                WHERE token = %s
            ),
            creator_stats AS (
                SELECT
                    COALESCE(tokens_created, 0) as tokens_created,
                    COALESCE(tokens_graduated, 0) as tokens_graduated
                FROM launchpad_users
                WHERE address = (SELECT creator FROM token_data)
            )
            SELECT
                t.token, t.creator, t.name, t.symbol, t.metadata_cid, t.description,
                t.social1, t.social2, t.social3, t.social4, t.source,
                t.created_block, t.created_at, t.migrated, t.migrated_block, t.migrated_at,
                t.market, t.last_price_native, t.native_volume, t.token_volume,
                t.volume_usd, t.fees_usd, t.buy_count, t.sell_count, t.tx_count,
                t.circulating_supply, t.snipers_count, t.approaching_75,
                t.approaching_75_block, t.approaching_75_at,
                h.holder_count,
                s.volume_native_24h, s.volume_usd_24h, s.buys_24h, s.sells_24h,
                b.distinct_buyers, b.distinct_sellers,
                COALESCE(c.tokens_created, 0), COALESCE(c.tokens_graduated, 0)
            FROM token_data t
            CROSS JOIN holder_stats h
            CROSS JOIN trade_stats_24h s
            CROSS JOIN buyer_seller_counts b
            LEFT JOIN creator_stats c ON true
        """, (token_addr, excluded_list, token_addr, token_addr, day_ago, excluded_list, excluded_list, token_addr))
        row = cur.fetchone()

    if not row:
        return None

    return {
        "token": row[0], "creator": (row[1] or "").lower(), "name": row[2], "symbol": row[3],
        "metadata_cid": row[4], "description": row[5], "social1": row[6], "social2": row[7],
        "social3": row[8], "social4": row[9], "source": row[10], "created_block": row[11],
        "created_at": row[12], "migrated": row[13], "migrated_block": row[14], "migrated_at": row[15],
        "market": row[16], "last_price_native": row[17] or Decimal(0), "native_volume": row[18],
        "token_volume": row[19], "volume_usd": row[20], "fees_usd": row[21], "buy_count": row[22],
        "sell_count": row[23], "tx_count": row[24], "circulating_supply": row[25], "snipers_count": row[26],
        "approaching_75": row[27], "approaching_75_block": row[28], "approaching_75_at": row[29],
        "holder_count": row[30], "volume_native_24h": row[31], "volume_usd_24h": row[32],
        "buys_24h": row[33], "sells_24h": row[34], "distinct_buyers": row[35], "distinct_sellers": row[36],
        "dev_tokens_created": row[37], "dev_tokens_graduated": row[38],
    }


@app.get("/token/{token_addr}/{chartres}")
def token_overview_graph(
    token_addr: str,
    chartres: int,
    tracked: str = Query(
        "",
        description="comma-separated list of addresses to track for trackedtrades",
    ),
) -> Dict[str, Any]:
    t0 = time.time()
    excluded = _internal_addrs()

    try:
        if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
            raise HTTPException(status_code=400)

        token_addr = token_addr.lower()
        now_ts = int(time.time())
        day_ago = now_ts - 86400

        core = _get_token_core_stats(token_addr, day_ago, excluded)
        if core is None:
            raise HTTPException(status_code=404)

        token = core["token"]
        creator = core["creator"]
        name = core["name"]
        symbol = core["symbol"]
        metadata_cid = core["metadata_cid"]
        description = core["description"]
        social1 = core["social1"]
        social2 = core["social2"]
        social3 = core["social3"]
        social4 = core["social4"]
        source = core["source"]
        created_at = core["created_at"]
        migrated = core["migrated"]
        migrated_at = core["migrated_at"]
        market = core["market"]
        last_price_native = core["last_price_native"]
        circulating_supply = core["circulating_supply"]
        snipers_count = core["snipers_count"]

        mon_price = _mon_price_usd()

        holders_count = int(core["holder_count"] or 0)
        distinct_buyers = int(core["distinct_buyers"] or 0)
        distinct_sellers = int(core["distinct_sellers"] or 0)
        dev_tokens_created = int(core["dev_tokens_created"] or 0)
        dev_tokens_graduated = int(core["dev_tokens_graduated"] or 0)
        volume_native_24h = int(core["volume_native_24h"] or 0)
        volume_usd_24h = core["volume_usd_24h"] or Decimal(0)
        buys_24h = int(core["buys_24h"] or 0)
        sells_24h = int(core["sells_24h"] or 0)

        _, dev_holding, _top10, top10_addresses = _holders_for_token(token_addr, creator)

        decimals = 18
        last_price_wad = last_price_native * Decimal(1e9)
        marketcap_native_raw = last_price_native * Decimal(1e9)
        marketcap_usd = marketcap_native_raw * mon_price if mon_price > 0 else Decimal(0)

        mini_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=3600, max_buckets=24)
        series_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=chartres, max_buckets=None)

        holders_list: List[Dict[str, Any]] = []

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    user_address,
                    balance_token,
                    native_spent,
                    native_received,
                    realized_pnl_native,
                    unrealized_pnl_native,
                    total_pnl_native,
                    trade_count,
                    buy_count,
                    sell_count,
                    token_bought,
                    token_sold
                FROM launchpad_positions
                WHERE token = %s AND balance_token > 0 AND user_address <> ALL(%s)
                ORDER BY balance_token DESC
                LIMIT 50
                """,
                (token_addr, list(excluded)),
            )
            pos_rows = cur.fetchall()

        for (
            user_address,
            balance_token,
            native_spent,
            native_received,
            realized_pnl_native,
            unrealized_pnl_native,
            total_pnl_native,
            trade_count,
            buy_count,
            sell_count,
            token_bought,
            token_sold,
        ) in pos_rows:
            balance_token = int(balance_token or 0)
            native_spent = int(native_spent or 0)
            native_received = int(native_received or 0)
            realized_pnl = realized_pnl_native or Decimal(0)
            unrealized_pnl = unrealized_pnl_native or Decimal(0)
            total_pnl = total_pnl_native or (realized_pnl + unrealized_pnl)

            current_value_native = Decimal(balance_token) * last_price_native

            if mon_price > 0:
                balance_usd = current_value_native * mon_price
                total_pnl_usd = total_pnl * mon_price
            else:
                balance_usd = Decimal(0)
                total_pnl_usd = Decimal(0)

            holders_list.append(
                {
                    "account": {"id": user_address},
                    "token": token_addr,
                    "symbol": symbol,
                    "name": name,
                    "metadata_cid": metadata_cid or "",
                    "balance_token": str(balance_token),
                    "balance_native": _fmt(current_value_native),
                    "balance_usd": _fmt_usd(balance_usd),
                    "native_spent": str(native_spent),
                    "native_received": str(native_received),
                    "realized_pnl_native": _fmt(realized_pnl),
                    "unrealized_pnl_native": _fmt(unrealized_pnl),
                    "total_pnl_native": _fmt(total_pnl),
                    "total_pnl_usd": _fmt_usd(total_pnl_usd),
                    "trade_count": int(trade_count or 0),
                    "buy_count": int(buy_count or 0),
                    "sell_count": int(sell_count or 0),
                    "tokens": str(balance_token),
                    "tokenBought": str(int(token_bought or 0)),
                    "tokenSold": str(int(token_sold or 0)),
                    "nativeSpent": str(native_spent),
                    "nativeReceived": str(native_received),
                }
            )

        top_traders_list: List[Dict[str, Any]] = []

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    user_address,
                    balance_token,
                    native_spent,
                    native_received,
                    realized_pnl_native,
                    unrealized_pnl_native,
                    total_pnl_native,
                    trade_count,
                    buy_count,
                    sell_count,
                    token_bought,
                    token_sold
                FROM launchpad_positions
                WHERE token = %s AND user_address <> ALL(%s)
                ORDER BY total_pnl_native DESC
                LIMIT 50
                """,
                (token_addr, list(excluded)),
            )
            trader_rows = cur.fetchall()

        for (
            user_address,
            balance_token,
            native_spent,
            native_received,
            realized_pnl_native,
            unrealized_pnl_native,
            total_pnl_native,
            trade_count,
            buy_count,
            sell_count,
            token_bought,
            token_sold,
        ) in trader_rows:
            balance_token = int(balance_token or 0)
            native_spent = int(native_spent or 0)
            native_received = int(native_received or 0)
            realized_pnl = realized_pnl_native or Decimal(0)
            unrealized_pnl = unrealized_pnl_native or Decimal(0)
            total_pnl = total_pnl_native or (realized_pnl + unrealized_pnl)

            current_value_native = Decimal(balance_token) * last_price_native

            if mon_price > 0:
                balance_usd = current_value_native * mon_price
                total_pnl_usd = total_pnl * mon_price
            else:
                balance_usd = Decimal(0)
                total_pnl_usd = Decimal(0)

            top_traders_list.append(
                {
                    "account": {"id": user_address},
                    "token": token_addr,
                    "symbol": symbol,
                    "name": name,
                    "metadata_cid": metadata_cid or "",
                    "balance_token": str(balance_token),
                    "balance_native": _fmt(current_value_native),
                    "balance_usd": _fmt_usd(balance_usd),
                    "native_spent": str(native_spent),
                    "native_received": str(native_received),
                    "realized_pnl_native": _fmt(realized_pnl),
                    "unrealized_pnl_native": _fmt(unrealized_pnl),
                    "total_pnl_native": _fmt(total_pnl),
                    "total_pnl_usd": _fmt_usd(total_pnl_usd),
                    "trade_count": int(trade_count or 0),
                    "buy_count": int(buy_count or 0),
                    "sell_count": int(sell_count or 0),
                    "tokens": str(balance_token),
                    "tokenBought": str(int(token_bought or 0)),
                    "tokenSold": str(int(token_sold or 0)),
                    "nativeSpent": str(native_spent),
                    "nativeReceived": str(native_received),
                }
            )

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    log_index,
                    timestamp,
                    user_address,
                    is_buy,
                    native_amount,
                    token_amount,
                    price_native,
                    txhash
                FROM launchpad_trades
                WHERE token = %s
                ORDER BY timestamp DESC
                LIMIT 50
                """,
                (token_addr,),
            )
            trade_rows = cur.fetchall()

        recent_trades_raw = trade_rows
        trades_out: List[Dict[str, Any]] = []

        for log_index, ts_tr, user_address, is_buy, native_amount, token_amount, price_native, txhash in recent_trades_raw:
            is_buy_flag = bool(is_buy)
            native_amount = int(native_amount or 0)
            token_amount = int(token_amount or 0)

            if is_buy_flag:
                amount_in = native_amount
                amount_out = token_amount
            else:
                amount_in = token_amount
                amount_out = native_amount

            trades_out.append(
                {
                    "trade": {
                        "account": {"id": user_address},
                        "amountIn": str(amount_in),
                        "amountOut": str(amount_out),
                        "block": str(int(ts_tr)),
                        "id": f"{txhash}-{log_index}",
                        "isBuy": is_buy_flag,
                        "priceNativePerTokenWad": str(price_native or Decimal(0)),
                    }
                }
            )

        tracked_addrs: set[str] = set()
        if tracked:
            for part in tracked.split(","):
                a = part.strip().lower()
                if a:
                    tracked_addrs.add(a)

        tracked_trades_out: List[Dict[str, Any]] = []
        if tracked_addrs and recent_trades_raw:
            for log_index, ts_tr, user_address, is_buy, native_amount, token_amount, price_native, txhash in recent_trades_raw:
                if user_address.lower() not in tracked_addrs:
                    continue

                is_buy_flag = bool(is_buy)
                native_amount = int(native_amount or 0)
                token_amount = int(token_amount or 0)

                if is_buy_flag:
                    amount_in = native_amount
                    amount_out = token_amount
                else:
                    amount_in = token_amount
                    amount_out = native_amount

                tracked_trades_out.append(
                    {
                        "trade": {
                            "account": {"id": user_address},
                            "amountIn": str(amount_in),
                            "amountOut": str(amount_out),
                            "block": str(int(ts_tr)),
                            "id": f"{txhash}-{log_index}",
                            "isBuy": is_buy_flag,
                            "priceNativePerTokenWad": str(price_native or Decimal(0)),
                        }
                    }
                )
                if len(tracked_trades_out) >= 50:
                    break

        if trade_rows:
            last_timestamp = int(trade_rows[0][1])
        else:
            last_timestamp = int(created_at or 0) or int(time.time())

        description_val = description or ""
        metadata_cid_val = metadata_cid or ""

        migrated_flag = bool(migrated)

        dev_tokens_list: List[Dict[str, Any]] = []
        dev_tokens_total = 0
        if creator:
            cutoff_ts = now_ts - 3600

            with db_cursor() as cur:
                cur.execute("""
                    SELECT
                        t.token, t.name, t.symbol, t.metadata_cid, t.last_price_native,
                        t.migrated, t.created_at, t.market, t.source,
                        COALESCE(v.vol_1h, 0) as vol_1h,
                        COALESCE(h.holder_count, 0) as holder_count
                    FROM launchpad_tokens t
                    LEFT JOIN LATERAL (
                        SELECT COALESCE(SUM(native_amount), 0) as vol_1h
                        FROM launchpad_trades
                        WHERE token = t.token AND timestamp >= %s
                    ) v ON true
                    LEFT JOIN LATERAL (
                        SELECT COUNT(*) as holder_count
                        FROM launchpad_positions
                        WHERE token = t.token AND balance_token > 1
                    ) h ON true
                    WHERE t.creator = %s
                    ORDER BY t.created_at DESC NULLS LAST
                    LIMIT 50
                """, (cutoff_ts, creator))
                dev_token_rows = cur.fetchall()
                dev_tokens_total = len(dev_token_rows)

            for row in dev_token_rows:
                dev_last_price = row[4] or Decimal(0)
                dev_price_wad = dev_last_price * Decimal(1e9)
                dev_tokens_list.append({
                    "id": row[0],
                    "name": row[1],
                    "symbol": row[2],
                    "metadataCID": row[3] or "",
                    "lastPriceNativePerTokenWad": str(dev_price_wad),
                    "marketcap": str(dev_price_wad),
                    "migrated": bool(row[5]),
                    "volumeNative1h": str(int(row[9] or 0)),
                    "holders": int(row[10] or 0),
                    "timestamp": str(int(row[6] or 0)),
                    "market": row[7] or None,
                    "source": int(row[8] or 0),
                })

        graduation_bps = (circulating_supply or 0) / 793100000

        sniper_addresses: List[str] = []
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
        for (addr,) in sniper_rows:
            if addr:
                sniper_addresses.append(addr)

        sniper_balance = 0
        if sniper_addresses:
            with db_cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(balance_token), 0)
                    FROM launchpad_positions
                    WHERE token = %s AND user_address = ANY(%s)
                    """,
                    (token_addr, sniper_addresses),
                )
                sb_row = cur.fetchone()
            sniper_balance = int(sb_row[0] or 0)

        sniper_share = float(Decimal(sniper_balance) / Decimal(1_000_000_000)) if sniper_balance > 0 else 0.0

        snipers_view = {
            "count": int(snipers_count or len(sniper_addresses)),
            "addresses": sorted(list({a for a in sniper_addresses})),
            "holdingShare": sniper_share,
        }
        
        result = {
            "buyTxs": buys_24h,
            "creator": {
                "id": creator,
                "tokensGraduated": int(dev_tokens_graduated),
                "tokensLaunched": int(dev_tokens_created),
            },
            "decimals": int(decimals),
            "description": description_val,
            "devHoldingAmount": str(int(dev_holding)),
            "distinctBuyers": distinct_buyers,
            "distinctSellers": distinct_sellers,
            "holders": holders_list,
            "topTraders": top_traders_list,
            "devTokens": dev_tokens_list,
            "devTokensTotal": dev_tokens_total,
            "id": token_addr,
            "initialSupply": str(10**18),
            "lastPriceNativePerTokenWad": str(last_price_wad),
            "lastUpdatedAt": str(last_timestamp),
            "market": market,
            "marketcap": marketcap_native_raw,
            "marketcap_usd": marketcap_usd,
            "metadataCID": metadata_cid_val,
            "migrated": migrated_flag,
            "migratedAt": migrated_at,
            "migratedMarket": market,
            "mini": {
                "klines": mini_klines,
            },
            "name": name,
            "sellTxs": sells_24h,
            "series": {
                "klines": series_klines,
            },
            "snipers": snipers_view,
            "social1": social1,
            "social2": social2,
            "social3": social3,
            "social4": social4,
            "symbol": symbol,
            "timestamp": str(int(created_at or 0)),
            "totalHolders": int(holders_count),
            "top10Addresses": top10_addresses,
            "trackedtrades": tracked_trades_out,
            "trades": trades_out,
            "volumeNative": str(volume_native_24h),
            "volumeUsd": _fmt_usd(volume_usd_24h),
            "graduationPercentageBps": graduation_bps,
            "circulating_supply": str(int(circulating_supply or 0)),
            "source": int(source or 0),
        }
        
        return result
    except Exception:
        print(f"[token_overview_graph] error token={token_addr}")
        traceback.print_exc()
        raise
    finally:
        dt = (time.time() - t0) * 1000
        log.info("token_overview_graph token=%s chartres=%s dt_ms=%.1f", token_addr, chartres, dt)


@app.get("/user/{user_addr}")
def user_portfolio(user_addr: str) -> Dict[str, Any]:
    user_addr = user_addr.lower()
    mon_price = _mon_price_usd()

    positions: List[Dict[str, Any]] = []

    total_value_native = Decimal(0)
    total_realized_pnl = Decimal(0)
    total_unrealized_pnl = Decimal(0)
    total_native_spent = 0
    total_native_received = 0
    total_trades = 0

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                p.token,
                p.token_bought,
                p.token_sold,
                p.native_spent,
                p.native_received,
                p.balance_token,
                p.realized_pnl_native,
                p.unrealized_pnl_native,
                p.total_pnl_native,
                p.trade_count,
                p.buy_count,
                p.sell_count,
                t.name,
                t.symbol,
                t.metadata_cid,
                t.last_price_native,
                t.market,
                t.source
            FROM launchpad_positions p
            JOIN launchpad_tokens t ON t.token = p.token
            WHERE p.user_address = %s
            """,
            (user_addr,),
        )
        pos_rows = cur.fetchall()

    for (
        token,
        token_bought,
        token_sold,
        native_spent,
        native_received,
        balance_token,
        realized_pnl_native,
        unrealized_pnl_native,
        total_pnl_native,
        trade_count,
        buy_count,
        sell_count,
        name,
        symbol,
        metadata_cid,
        last_price_native,
        market,
        source,
    ) in pos_rows:
        last_price_native = last_price_native or Decimal(0)
        token_bought = int(token_bought or 0)
        token_sold = int(token_sold or 0)
        balance_token = int(balance_token or 0)
        native_spent = int(native_spent or 0)
        native_received = int(native_received or 0)
        realized_pnl = realized_pnl_native or Decimal(0)
        unrealized_pnl = unrealized_pnl_native or Decimal(0)
        total_pnl = total_pnl_native or (realized_pnl + unrealized_pnl)

        current_value_native = Decimal(balance_token) * last_price_native
        unrealized_pnl_val = unrealized_pnl

        total_value_native += current_value_native
        total_realized_pnl += realized_pnl
        total_unrealized_pnl += unrealized_pnl_val
        total_native_spent += native_spent
        total_native_received += native_received
        total_trades += int(trade_count or 0)

        if mon_price > 0:
            current_value_usd = current_value_native * mon_price
            total_pnl_usd = total_pnl * mon_price
        else:
            current_value_usd = Decimal(0)
            total_pnl_usd = Decimal(0)

        positions.append(
            {
                "token": token,
                "symbol": symbol,
                "name": name,
                "metadata_cid": metadata_cid or "",
                "balance_token": str(balance_token),
                "balance_native": _fmt(current_value_native),
                "balance_usd": _fmt_usd(current_value_usd),
                "native_spent": str(native_spent),
                "native_received": str(native_received),
                "realized_pnl_native": _fmt(realized_pnl),
                "unrealized_pnl_native": _fmt(unrealized_pnl_val),
                "total_pnl_native": _fmt(total_pnl),
                "total_pnl_usd": _fmt_usd(total_pnl_usd),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
                "token_bought": str(token_bought),
                "token_sold": str(token_sold),
                "market": market or None,
                "source": int(source or 0),
            }
        )

    positions.sort(
        key=lambda p: Decimal(p["total_pnl_native"]) if p["total_pnl_native"] is not None else Decimal(0),
        reverse=True,
    )

    if mon_price > 0:
        total_value_usd = total_value_native * mon_price
        total_pnl_native_val = total_realized_pnl + total_unrealized_pnl
        total_pnl_usd = total_pnl_native_val * mon_price
    else:
        total_value_usd = Decimal(0)
        total_pnl_native_val = total_realized_pnl + total_unrealized_pnl
        total_pnl_usd = Decimal(0)

    summary = {
        "user": user_addr,
        "portfolio_value_native": _fmt(total_value_native),
        "portfolio_value_usd": _fmt_usd(total_value_usd),
        "realized_pnl_native": _fmt(total_realized_pnl),
        "unrealized_pnl_native": _fmt(total_unrealized_pnl),
        "total_pnl_native": _fmt(total_pnl_native_val),
        "total_pnl_usd": _fmt_usd(total_pnl_usd),
        "native_spent": str(total_native_spent),
        "native_received": str(total_native_received),
        "trade_count": int(total_trades),
        "tokens_traded": len(positions),
    }

    return {
        "user": user_addr,
        "summary": summary,
        "positions": positions,
    }


@app.get("/stats/{token_addr}")
def token_stats(token_addr: str) -> Dict[str, Any]:
    token_addr = token_addr.lower()

    windows = {
        "5m": 5 * 60,
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
    }

    out: Dict[str, Any] = {
        "type": "stats",
        "token": token_addr,
    }

    now_ts = int(time.time())
    INITIAL_NATIVE_PRICE = Decimal("0.00008387696")

    for label, secs in windows.items():
        suffix = label
        start_ts = now_ts - secs

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(usd_amount), 0),
                    COALESCE(SUM(usd_amount) FILTER (WHERE is_buy = TRUE), 0),
                    COALESCE(SUM(usd_amount) FILTER (WHERE is_buy = FALSE), 0),
                    COUNT(*) FILTER (WHERE is_buy = TRUE),
                    COUNT(*) FILTER (WHERE is_buy = FALSE)
                FROM launchpad_trades
                WHERE token = %s AND timestamp > %s AND timestamp <= %s
                """,
                (token_addr, start_ts, now_ts),
            )
            vol_row = cur.fetchone()

        volume_usd = vol_row[0] or Decimal(0)
        buy_volume_usd = vol_row[1] or Decimal(0)
        sell_volume_usd = vol_row[2] or Decimal(0)
        buy_tx_count = vol_row[3] or 0
        sell_tx_count = vol_row[4] or 0

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT price_native
                FROM launchpad_trades
                WHERE token = %s AND timestamp <= %s
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (token_addr, start_ts),
            )
            prev_row = cur.fetchone()

            cur.execute(
                """
                SELECT price_native
                FROM launchpad_trades
                WHERE token = %s AND timestamp > %s AND timestamp <= %s
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (token_addr, start_ts, now_ts),
            )
            start_row = cur.fetchone()

            cur.execute(
                """
                SELECT price_native
                FROM launchpad_trades
                WHERE token = %s AND timestamp > %s AND timestamp <= %s
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (token_addr, start_ts, now_ts),
            )
            last_row = cur.fetchone()

        prev_price = (prev_row[0] if prev_row else None) or None
        last_price = (last_row[0] if last_row else None) or None

        if last_price is not None:
            last_eff = last_price
        elif prev_price is not None:
            last_eff = prev_price
        else:
            last_eff = INITIAL_NATIVE_PRICE

        base_price = prev_price or INITIAL_NATIVE_PRICE

        if base_price == 0:
            change_pct = 0.0
        else:
            change_pct = float((last_eff - base_price) / base_price * Decimal(100))

        out[f"volume_usd_{suffix}"] = float(volume_usd)
        out[f"buy_volume_usd_{suffix}"] = float(buy_volume_usd)
        out[f"sell_volume_usd_{suffix}"] = float(sell_volume_usd)
        out[f"buy_tx_count_{suffix}"] = int(buy_tx_count)
        out[f"sell_tx_count_{suffix}"] = int(sell_tx_count)
        out[f"change_pct_{suffix}"] = change_pct

    return out


@app.get("/trades/{addresses}")
def trades_for_addresses(addresses: str) -> Dict[str, Any]:
    addrs = {a.strip().lower() for a in addresses.split(",") if a.strip()}
    if not addrs:
        raise HTTPException(status_code=400, detail="no addresses provided")

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                log_index,
                timestamp,
                user_address,
                is_buy,
                native_amount,
                token_amount,
                price_native,
                txhash,
                token
            FROM launchpad_trades
            WHERE user_address = ANY(%s)
            ORDER BY timestamp DESC
            LIMIT 50
            """,
            (list(addrs),),
        )
        rows = cur.fetchall()

    out: List[Dict[str, Any]] = []

    for log_index, ts_tr, user_address, is_buy, native_amount, token_amount, price_native, txhash, token in rows:
        is_buy_flag = bool(is_buy)
        native_amount = int(native_amount or 0)
        token_amount = int(token_amount or 0)

        if is_buy_flag:
            amount_in = native_amount
            amount_out = token_amount
        else:
            amount_in = token_amount
            amount_out = native_amount

        out.append(
            {
                "trade": {
                    "account": {"id": user_address},
                    "token": token,
                    "amountIn": str(amount_in),
                    "amountOut": str(amount_out),
                    "block": str(int(ts_tr)),
                    "id": f"{txhash}-{log_index}",
                    "isBuy": is_buy_flag,
                    "priceNativePerTokenWad": str(price_native or Decimal(0)),
                }
            }
        )

    return {
        "addresses": list(addrs),
        "count": len(out),
        "trades": out,
    }


@app.get("/chart/{token_addr}/{chartres}")
def chart_only(
    token_addr: str,
    chartres: int,
) -> Dict[str, Any]:
    token_addr = token_addr.lower()

    if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
        raise HTTPException(status_code=400)

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT bucket_start, open_price, high_price, low_price, close_price, quote_volume
            FROM launchpad_ohlcv
            WHERE token = %s AND resolution_sec = %s
            ORDER BY bucket_start DESC
            LIMIT 1000
            """,
            (token_addr, chartres),
        )
        rows = cur.fetchall()

    rows.reverse()

    out = []
    for bucket_start, open_p, high_p, low_p, close_p, qv in rows:
        open_wad = (open_p or Decimal(0)) * Decimal(1e9)
        high_wad = (high_p or Decimal(0)) * Decimal(1e9)
        low_wad = (low_p or Decimal(0)) * Decimal(1e9)
        close_wad = (close_p or Decimal(0)) * Decimal(1e9)
        quote_volume = int(qv or 0)

        out.append(
            {
                "time": str(int(bucket_start)),
                "open": str(int(open_wad)),
                "high": str(int(high_wad)),
                "low": str(int(low_wad)),
                "close": str(int(close_wad)),
                "quoteVolume": str(quote_volume),
            }
        )

    return {
        "token": token_addr,
        "resolution": chartres,
        "klines": out,
    }


@app.get("/volume/{user_addr}")
def user_volume(user_addr: str) -> Dict[str, Any]:
    user_addr = user_addr.lower()

    total_native_volume = 0
    total_trades = 0
    seen_tokens: set[str] = set()

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, native_spent, native_received, trade_count
            FROM launchpad_positions
            WHERE user_address = %s
            """,
            (user_addr,),
        )
        rows = cur.fetchall()

    for token, native_spent, native_received, trade_count in rows:
        native_spent = int(native_spent or 0)
        native_received = int(native_received or 0)
        trade_count = int(trade_count or 0)

        total_native_volume += native_spent + native_received
        total_trades += trade_count

        if trade_count > 0:
            seen_tokens.add(token)

    total_native_volume_dec = Decimal(total_native_volume)

    return {
        "user": user_addr,
        "volume_native": str(total_native_volume_dec),
        "trade_count": int(total_trades),
        "tokens_traded": len(seen_tokens),
    }


@app.get("/search/query")
def search_tokens_api(
    query: str = Query(
        ...,
        min_length=1,
        max_length=64,
        description="search string for token name, symbol, or address",
    ),
    sort: str = Query(
        None,
        description="optional sort: 'mc', 'volume_1h', 'volume_24h', 'recent', 'holders'",
    ),
) -> Dict[str, Any]:
    q = query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="empty query")

    excluded = _internal_addrs()

    if sort is None:
        rows = storage.search_tokens(q, limit=50)
        token_addrs = [(token or "").lower() for token, _, _ in rows if token]
        circ_map = {(token or "").lower(): circ for token, circ, _ in rows}

        if not token_addrs:
            return {"query": query, "sort": None, "count": 0, "results": []}

        token_data = _batch_serialize_tokens(token_addrs, excluded)
        results = []
        for t in token_addrs:
            if t in token_data:
                data = token_data[t]
                data["graduationPercentageBps"] = (circ_map.get(t) or 0) / 793100000
                results.append(data)

        return {"query": query, "sort": None, "count": len(results), "results": results}

    rows = storage.search_tokens(q, limit=1000)
    token_addrs = [(token or "").lower() for token, _, _ in rows if token]

    if not token_addrs:
        return {"query": query, "sort": sort, "count": 0, "results": []}

    now_ts = int(time.time())

    with db_cursor() as cur:
        if sort == "mc":
            cur.execute("""
                SELECT token, last_price_native, circulating_supply
                FROM launchpad_tokens
                WHERE token = ANY(%s)
                ORDER BY last_price_native DESC NULLS LAST
                LIMIT 50
            """, (token_addrs,))
        elif sort == "recent":
            cur.execute("""
                SELECT token, last_price_native, circulating_supply
                FROM launchpad_tokens
                WHERE token = ANY(%s)
                ORDER BY created_at DESC NULLS LAST
                LIMIT 50
            """, (token_addrs,))
        elif sort == "volume_1h":
            cutoff_1h = now_ts - 3600
            cur.execute("""
                SELECT t.token, t.last_price_native, t.circulating_supply
                FROM launchpad_tokens t
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(native_amount), 0) as vol
                    FROM launchpad_trades
                    WHERE token = t.token AND timestamp >= %s
                ) tr ON true
                WHERE t.token = ANY(%s)
                ORDER BY tr.vol DESC NULLS LAST
                LIMIT 50
            """, (cutoff_1h, token_addrs))
        elif sort == "volume_24h":
            cutoff_24h = now_ts - 86400
            cur.execute("""
                SELECT t.token, t.last_price_native, t.circulating_supply
                FROM launchpad_tokens t
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(native_amount), 0) as vol
                    FROM launchpad_trades
                    WHERE token = t.token AND timestamp >= %s
                ) tr ON true
                WHERE t.token = ANY(%s)
                ORDER BY tr.vol DESC NULLS LAST
                LIMIT 50
            """, (cutoff_24h, token_addrs))
        elif sort == "holders":
            cur.execute("""
                SELECT t.token, t.last_price_native, t.circulating_supply
                FROM launchpad_tokens t
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) as cnt
                    FROM launchpad_positions
                    WHERE token = t.token AND balance_token > 1
                ) p ON true
                WHERE t.token = ANY(%s)
                ORDER BY p.cnt DESC NULLS LAST
                LIMIT 50
            """, (token_addrs,))
        else:
            raise HTTPException(status_code=400, detail=f"invalid sort: {sort}. Use 'mc', 'volume_1h', 'volume_24h', 'recent', or 'holders'")

        sorted_rows = cur.fetchall()

    sorted_addrs = [row[0].lower() for row in sorted_rows if row[0]]
    circ_map = {row[0].lower(): row[2] for row in sorted_rows if row[0]}
    token_data = _batch_serialize_tokens(sorted_addrs, excluded)

    results = []
    for t in sorted_addrs:
        if t in token_data:
            data = token_data[t]
            data["graduationPercentageBps"] = (circ_map.get(t) or 0) / 793100000
            results.append(data)

    return {"query": query, "sort": sort, "count": len(results), "results": results}


_mon_price_cache: tuple[float, Decimal] | None = None

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


@app.get("/debug/mon_price")
def get_mon_price() -> Decimal:
    return(_mon_price_usd())


@app.get("/sync")
def get_sync_status() -> Dict[str, Any]:
    last_block = storage.get_last_processed_block()
    return {
        "last_block": last_block,
    }
