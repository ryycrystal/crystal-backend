from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query

from api.api import (
    _PCT_OF_SUPPLY,
    _WEI,
    _api_source,
    _apply_live_pool_reserves,
    _batch_serialize_tokens,
    _build_ohlcv_from_db,
    _encode_cursor,
    _fmt,
    _fmt_usd,
    _holders_for_token,
    _lifecycle_fields,
    _mon_price_usd,
    _nadfun_version,
    _parse_history_cursor,
    _parse_positions_cursor,
    _quote_price_usd,
    _scaled_price,
    _sql_not_internal,
    _static_internal_addrs,
    db_cursor,
    log,
    storage,
    time,
    ttl_cache,
)

router = APIRouter()


def _csv(raw: str) -> list[str]:
    return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]


from api.api import _curve_supply_for


def _true_source(data) -> int:
    v = int(data.get("nadfunVersion") or 0)
    return v if v in (1, 2) else int(data.get("source") or 0)


def _graduation_pct(circulating, source) -> float:
    try:
        denom = _curve_supply_for(source) // 10**18
        if denom <= 0:
            return 0.0
        return float(circulating or 0) / denom
    except Exception:
        return 0.0


@router.get("/holders/{token_addr}")
def token_holders(
    token_addr: str,
    q: str = Query("", description="address substring, matched server side"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = Query("balance", pattern="^(balance|pnl)$"),
) -> dict[str, Any]:
    token_addr = token_addr.lower()
    needle = (q or "").strip().lower()

    where = ["token = %s", "balance_token > 0", "user_address <> ALL(%s)", _sql_not_internal("user_address")]
    params: list[Any] = [token_addr, list(_static_internal_addrs())]
    if needle:
        where.append("user_address LIKE %s")
        params.append(f"%{needle}%")
    clause = " AND ".join(where)

    order = "balance_token DESC" if sort == "balance" else "total_pnl_native DESC"

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM launchpad_positions WHERE {clause}", tuple(params))
        row = cur.fetchone()
        total = int(row[0]) if row else 0

        cur.execute(
            f"""
            SELECT user_address, balance_token, native_spent, native_received,
                   realized_pnl_native, unrealized_pnl_native, total_pnl_native,
                   trade_count, buy_count, sell_count, token_bought, token_sold
            FROM launchpad_positions_live
            WHERE {clause}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            tuple(params + [int(limit), int(offset)]),
        )
        rows = cur.fetchall()

    with db_cursor() as cur:
        cur.execute(
            "SELECT symbol, name, last_price_native, quote_token FROM launchpad_tokens WHERE token = %s",
            (token_addr,),
        )
        core_row = cur.fetchone()
    symbol = (core_row[0] if core_row else "") or ""
    name = (core_row[1] if core_row else "") or ""
    last_price_native = (core_row[2] if core_row else None) or Decimal(0)
    quote_price_usd = _quote_price_usd(core_row[3] if core_row else None)

    holders: list[dict[str, Any]] = []
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
    ) in rows:
        balance_token = int(balance_token or 0)
        realized_pnl = realized_pnl_native or Decimal(0)
        unrealized_pnl = unrealized_pnl_native or Decimal(0)
        total_pnl = total_pnl_native or (realized_pnl + unrealized_pnl)
        current_value_native = Decimal(balance_token) * last_price_native

        holders.append(
            {
                "account": {"id": user_address},
                "token": token_addr,
                "symbol": symbol,
                "name": name,
                "balance_token": str(balance_token),
                "balance_native": _fmt(current_value_native),
                "balance_usd": _fmt_usd(current_value_native * quote_price_usd / _WEI) if quote_price_usd > 0 else "0",
                "native_spent": str(int(native_spent or 0)),
                "native_received": str(int(native_received or 0)),
                "realized_pnl_native": _fmt(realized_pnl),
                "unrealized_pnl_native": _fmt(unrealized_pnl),
                "total_pnl_native": _fmt(total_pnl),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
                "tokenBought": str(int(token_bought or 0)),
                "tokenSold": str(int(token_sold or 0)),
            }
        )

    return {
        "token": token_addr,
        "query": needle,
        "sort": sort,
        "limit": int(limit),
        "offset": int(offset),
        "total": total,
        "count": len(holders),
        "holders": holders,
    }


@ttl_cache("tokens:list", ttl_seconds=3, serve_stale_seconds=30)
def _list_tokens_cached(since_block: int, since: int) -> dict[str, Any]:
    return _list_tokens_impl(since_block, since, {})


@router.get("/tokens")
def list_tokens(
    since_block: int = Query(0, ge=0, description="only tokens touched after this block"),
    since: int = Query(0, ge=0, description="same, as a unix timestamp. block is exact, prefer it"),
    exclude_dev: str = Query("", description="comma separated creator addresses"),
    exclude_ca: str = Query("", description="comma separated token addresses"),
    exclude_website: str = Query("", description="comma separated bare hostnames"),
    exclude_twitter: str = Query("", description="comma separated bare handles"),
    filters: str = Query("", description="url encoded json: per-bucket search filters"),
) -> dict[str, Any]:
    ex = {
        "exclude_dev": _csv(exclude_dev),
        "exclude_ca": _csv(exclude_ca),
        "exclude_website": _csv(exclude_website),
        "exclude_twitter": _csv(exclude_twitter),
    }
    if filters:
        try:
            per_bucket = json.loads(filters)
            assert isinstance(per_bucket, dict)
        except Exception:
            raise HTTPException(status_code=400, detail="filters must be a json object keyed by bucket") from None
        phase_by_bucket = {"new": "new", "approaching": "graduating", "graduated": "graduated"}
        out: dict[str, Any] = {}
        applied: dict[str, list] = {}
        for bucket, phase in phase_by_bucket.items():
            f = dict(per_bucket.get(bucket) or {})
            f.update(ex)
            f["phase"] = phase
            res = _search_impl(str(f.pop("query", "") or ""), str(f.pop("sort", "") or ""), 30, 0, f)
            key = {"new": "recent_created", "approaching": "recent_approaching", "graduated": "recent_graduated"}[
                bucket
            ]
            out[key] = res["results"]
            out[key + "_total"] = res["total"]
            applied[bucket] = res["applied_filters"]
        out["applied_filters"] = applied
        out["as_of_block"] = storage.get_last_processed_block() or 0
        return out
    if not any(ex.values()):
        return _list_tokens_cached(since_block, since)
    return _list_tokens_impl(since_block, since, ex)


def _list_tokens_impl(since_block: int, since: int, ex: dict) -> dict[str, Any]:
    t0 = time.time()
    bl_where, bl_params = storage.blacklist_clauses(ex, "")
    bl_sql = ("".join(f" AND {c}" for c in bl_where)) if bl_where else ""
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT token, circulating_supply
            FROM launchpad_tokens
            WHERE migrated = TRUE{bl_sql}
            ORDER BY migrated_at DESC NULLS LAST, migrated_block DESC NULLS LAST
            LIMIT 30
        """,
            tuple(bl_params),
        )
        grad_rows = cur.fetchall()

        graduated_ids = {t.lower() for (t, _) in grad_rows if t}

        if graduated_ids:
            cur.execute(
                f"""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                  AND migrated = FALSE
                  AND token <> ALL(%s){bl_sql}
                ORDER BY circulating_supply DESC
                LIMIT 30
            """,
                (list(graduated_ids),) + tuple(bl_params),
            )
        else:
            cur.execute(
                f"""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                  AND migrated = FALSE{bl_sql}
                ORDER BY circulating_supply DESC
                LIMIT 30
            """,
                tuple(bl_params),
            )
        appr_rows = cur.fetchall()

        approaching_ids = {t.lower() for (t, _) in appr_rows if t}
        excluded_ids = graduated_ids | approaching_ids

        if excluded_ids:
            cur.execute(
                f"""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE token <> ALL(%s){bl_sql}
                ORDER BY created_at DESC NULLS LAST, created_block DESC NULLS LAST
                LIMIT 30
            """,
                (list(excluded_ids),) + tuple(bl_params),
            )
        else:
            cur.execute(
                f"""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                {"WHERE " + " AND ".join(bl_where) if bl_where else ""}
                ORDER BY created_at DESC NULLS LAST, created_block DESC NULLS LAST
                LIMIT 30
            """,
                tuple(bl_params),
            )
        created_rows = cur.fetchall()

    all_token_addrs = [t for t, _ in grad_rows + appr_rows + created_rows]
    circ_map = {t: c for t, c in grad_rows + appr_rows + created_rows}
    token_data = _batch_serialize_tokens(all_token_addrs)

    def with_graduation_pct(token_addr):
        data = token_data.get(token_addr, {})
        if data:
            data["graduationPercentageBps"] = _graduation_pct(circ_map.get(token_addr), _true_source(data))
        return data

    recent_graduated_out = [with_graduation_pct(t) for t, _ in grad_rows if t in token_data]
    recent_approaching_out = [with_graduation_pct(t) for t, _ in appr_rows if t in token_data]
    recent_created_out = [with_graduation_pct(t) for t, _ in created_rows if t in token_data]

    result: dict[str, Any] = {
        "recent_created": recent_created_out,
        "recent_approaching": recent_approaching_out,
        "recent_graduated": recent_graduated_out,
    }

    if since_block or since:
        changed = storage.tokens_changed_since(all_token_addrs, since_block=since_block, since_ts=since)
        result["ids"] = {
            "recent_created": [t for t, _ in created_rows],
            "recent_approaching": [t for t, _ in appr_rows],
            "recent_graduated": [t for t, _ in grad_rows],
        }
        for bucket in ("recent_created", "recent_approaching", "recent_graduated"):
            result[bucket] = [r for r in result[bucket] if (r.get("token") or "").lower() in changed]
        result["since_block"] = since_block
        result["since"] = since

    result["as_of_block"] = storage.get_last_processed_block() or 0

    dt = (time.time() - t0) * 1000
    log.info("token_list dt_ms=%.1f", dt)

    return result


def _get_token_core_stats(token_addr: str, day_ago: int) -> dict | None:
    excluded_list = list(_static_internal_addrs())
    not_internal = _sql_not_internal("user_address")
    with db_cursor() as cur:
        cur.execute(
            f"""
            WITH token_data AS (
                SELECT * FROM launchpad_tokens WHERE token = %s
            ),
            holder_stats AS (
                SELECT
                    COUNT(*) as holder_count
                FROM launchpad_positions
                WHERE token = %s AND balance_token > 1
                  AND user_address <> ALL(%s) AND {not_internal}
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
                    COUNT(*) FILTER (WHERE buy_count > 0) as distinct_buyers,
                    COUNT(*) FILTER (WHERE sell_count > 0) as distinct_sellers
                FROM launchpad_positions
                WHERE token = %s
                  AND user_address <> ALL(%s) AND {not_internal}
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
                COALESCE(c.tokens_created, 0), COALESCE(c.tokens_graduated, 0),
                COALESCE(t.quote_token, '0x3bd359c1119da7da1d913d1c4d2b7c461115433a'),
                COALESCE(t.ath_price_native, 0),
                COALESCE(t.curve_native_reserve, 0),
                COALESCE(t.curve_token_reserve, 0)
            FROM token_data t
            CROSS JOIN holder_stats h
            CROSS JOIN trade_stats_24h s
            CROSS JOIN buyer_seller_counts b
            LEFT JOIN creator_stats c ON true
        """,
            (token_addr, token_addr, excluded_list, token_addr, day_ago, token_addr, excluded_list),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "token": row[0],
        "creator": (row[1] or "").lower(),
        "name": row[2],
        "symbol": row[3],
        "metadata_cid": row[4],
        "description": row[5],
        "social1": row[6],
        "social2": row[7],
        "social3": row[8],
        "social4": row[9],
        "source": row[10],
        "created_block": row[11],
        "created_at": row[12],
        "migrated": row[13],
        "migrated_block": row[14],
        "migrated_at": row[15],
        "market": row[16],
        "last_price_native": row[17] or Decimal(0),
        "native_volume": row[18],
        "token_volume": row[19],
        "volume_usd": row[20],
        "fees_usd": row[21],
        "buy_count": row[22],
        "sell_count": row[23],
        "tx_count": row[24],
        "circulating_supply": row[25],
        "snipers_count": row[26],
        "approaching_75": row[27],
        "approaching_75_block": row[28],
        "approaching_75_at": row[29],
        "holder_count": row[30],
        "volume_native_24h": row[31],
        "volume_usd_24h": row[32],
        "buys_24h": row[33],
        "sells_24h": row[34],
        "distinct_buyers": row[35],
        "distinct_sellers": row[36],
        "dev_tokens_created": row[37],
        "dev_tokens_graduated": row[38],
        "quote_token": (row[39] or "0x3bd359c1119da7da1d913d1c4d2b7c461115433a").lower(),
        "ath_price_native": row[40] or Decimal(0),
        "curve_native_reserve": row[41] or 0,
        "curve_token_reserve": row[42] or 0,
    }


@router.get("/token/{token_addr}/trades")
def token_trades_range(
    token_addr: str,
    from_ts: int = Query(0, alias="from", ge=0),
    to_ts: int = Query(0, alias="to", ge=0),
    limit: int = Query(500, ge=1, le=2000),
    callers: str = Query("", description="comma-separated addresses; server-side filter"),
) -> dict[str, Any]:
    token_addr = token_addr.lower()
    if to_ts <= 0:
        to_ts = int(time.time())
    if from_ts > to_ts:
        from_ts, to_ts = to_ts, from_ts

    caller_list = [c.strip().lower() for c in callers.split(",") if c.strip()]

    where = ["token = %s", "timestamp >= %s", "timestamp <= %s"]
    params: list[Any] = [token_addr, int(from_ts), int(to_ts)]
    if caller_list:
        where.append("user_address = ANY(%s)")
        params.append(caller_list)
    params.append(int(limit))

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT timestamp, block_number, log_index, user_address, is_buy,
                   native_amount, token_amount, usd_amount, price_native, txhash
            FROM launchpad_trades
            WHERE {" AND ".join(where)}
            ORDER BY timestamp DESC, log_index DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    rows.reverse()

    trades = [
        {
            "time": str(int(r[0])),
            "blockNumber": int(r[1]),
            "logIndex": int(r[2]),
            "caller": (r[3] or "").lower(),
            "isBuy": bool(r[4]),
            "nativeAmount": str(int(r[5] or 0)),
            "tokenAmount": str(int(r[6] or 0)),
            "usdAmount": _fmt_usd(r[7] or Decimal(0)),
            "price": _scaled_price(r[8]),
            "txhash": r[9],
        }
        for r in rows
    ]

    return {
        "token": token_addr,
        "from": int(from_ts),
        "to": int(to_ts),
        "limit": int(limit),
        "count": len(trades),
        "truncated": len(trades) >= limit,
        "trades": trades,
    }


@router.get("/token/{token_addr}/meta")
def token_meta(token_addr: str) -> dict[str, Any]:
    from core.adapters import nadfun as _nadfun_geo
    from modules.nadfun import fetch_pair_fee_config

    token_addr = token_addr.lower()
    with db_cursor() as cur:
        cur.execute("SELECT * FROM launchpad_tokens WHERE token = %s", (token_addr,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="token not found")
        cols = [d[0] for d in cur.description]
    raw = dict(zip(cols, row))
    for k, v in list(raw.items()):
        if isinstance(v, Decimal):
            raw[k] = _fmt(v)
        elif v is not None and not isinstance(v, (str, int, bool, list, dict)):
            raw[k] = str(v)

    source = int(raw.get("source") or 0)
    version = _nadfun_version(token_addr, source)
    market = (raw.get("market") or "").lower()

    fees: dict[str, Any] = {
        "curveFeeRate": _fmt(_nadfun_geo.fee_rate_for(source)) if _nadfun_geo.is_nadfun_source(source) else None,
        "pair": None,
        "crystalMarket": None,
    }
    if market and source != 0:
        cached = storage.get_pair_fees(market)
        if cached is None or (int(time.time()) - cached.get("fetchedAt", 0)) > 3600:
            cfg = fetch_pair_fee_config(market)
            storage.upsert_pair_fees(
                market,
                ok=cfg["ok"],
                fee_collector=cfg["fee_collector"],
                base_token=cfg["base_token"],
                quote_token=cfg["quote_token"],
                creator_fee_rate=cfg["creator_fee_rate"],
                curve_protocol_fee_rate=cfg["curve_protocol_fee_rate"],
                dex_protocol_fee_rate=cfg["dex_protocol_fee_rate"],
                pool_fee_ppm=cfg["pool_fee_ppm"],
                fetched_at=int(time.time()),
            )
            cached = storage.get_pair_fees(market)
        fees["pair"] = cached
    if market and source == 0:
        with db_cursor() as cur:
            cur.execute("SELECT taker_fee FROM crystal_markets WHERE LOWER(market) = %s", (market,))
            r = cur.fetchone()
        if r is not None:
            fees["crystalMarket"] = {"market": market, "takerFee": str(int(r[0] or 0))}

    reserves = {
        "migrated": bool(raw.get("migrated")),
        "market": market,
        "reserveQuote": str(int(Decimal(raw.get("curve_native_reserve") or 0))),
        "reserveBase": str(int(Decimal(raw.get("curve_token_reserve") or 0))),
    }
    _apply_live_pool_reserves({token_addr: reserves})

    return {
        "token": token_addr,
        "raw": raw,
        "source": _api_source(source),
        "sourceRaw": source,
        "nadfunVersion": version,
        **_lifecycle_fields(
            source=source,
            circulating_supply=raw.get("circulating_supply"),
            tx_count=raw.get("tx_count"),
            migrated=raw.get("migrated"),
        ),
        "reserveQuote": reserves["reserveQuote"],
        "reserveBase": reserves["reserveBase"],
        "reservesFrom": reserves.get("reservesFrom", "curve"),
        "reservesSyncedAt": reserves.get("reservesSyncedAt", 0),
        "fees": fees,
        "as_of_block": storage.get_last_processed_block() or 0,
    }


@router.get("/pair/{pair_addr}/fees")
def pair_fees(pair_addr: str) -> dict[str, Any]:
    from modules.nadfun import fetch_pair_fee_config

    pair_addr = pair_addr.lower()
    if not pair_addr.startswith("0x") or len(pair_addr) != 42:
        raise HTTPException(status_code=400, detail="invalid pair address")
    cached = storage.get_pair_fees(pair_addr)
    if cached is None or (int(time.time()) - cached.get("fetchedAt", 0)) > 3600:
        cfg = fetch_pair_fee_config(pair_addr)
        if cfg["ok"] or cached is None:
            storage.upsert_pair_fees(
                pair_addr,
                ok=cfg["ok"],
                fee_collector=cfg["fee_collector"],
                base_token=cfg["base_token"],
                quote_token=cfg["quote_token"],
                creator_fee_rate=cfg["creator_fee_rate"],
                curve_protocol_fee_rate=cfg["curve_protocol_fee_rate"],
                dex_protocol_fee_rate=cfg["dex_protocol_fee_rate"],
                pool_fee_ppm=cfg["pool_fee_ppm"],
                fetched_at=int(time.time()),
            )
        cached = storage.get_pair_fees(pair_addr)
    return cached or {"pair": pair_addr, "ok": False}


@router.get("/token/{token_addr}/{chartres}")
def token_overview_graph(
    token_addr: str,
    chartres: int,
    tracked: str = Query(
        "",
        description="comma-separated list of addresses to track for trackedtrades",
    ),
    series: bool = Query(
        True,
        description="set false to omit series.klines, which the client already has after first load",
    ),
) -> dict[str, Any]:
    from core.adapters import nadfun as _nadfun_geo

    t0 = time.time()
    excluded_static = list(_static_internal_addrs())

    try:
        if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
            raise HTTPException(status_code=400)

        token_addr = token_addr.lower()
        now_ts = int(time.time())
        day_ago = now_ts - 86400

        core = _get_token_core_stats(token_addr, day_ago)
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
        quote_token = core["quote_token"]
        circulating_supply = core["circulating_supply"]
        snipers_count = core["snipers_count"]

        quote_price_usd = _quote_price_usd(quote_token)

        holders_count = int(core["holder_count"] or 0)
        distinct_buyers = int(core["distinct_buyers"] or 0)
        distinct_sellers = int(core["distinct_sellers"] or 0)
        dev_tokens_created = int(core["dev_tokens_created"] or 0)
        dev_tokens_graduated = int(core["dev_tokens_graduated"] or 0)
        volume_native_24h = int(core["volume_native_24h"] or 0)
        volume_usd_24h = core["volume_usd_24h"] or Decimal(0)
        volume_native_lifetime = int(core["native_volume"] or 0)
        volume_usd_lifetime = core["volume_usd"] or Decimal(0)
        fees_usd_lifetime = core["fees_usd"] or Decimal(0)
        buys_24h = int(core["buys_24h"] or 0)
        sells_24h = int(core["sells_24h"] or 0)

        _, dev_holding, _top10, top10_addresses = _holders_for_token(token_addr, creator)

        decimals = 18
        last_price_wad = last_price_native * Decimal(1e9)
        marketcap_native_raw = last_price_native * Decimal(1e9)
        ath_price_native = core.get("ath_price_native") or Decimal(0)
        if ath_price_native < last_price_native:
            ath_price_native = last_price_native
        ath_marketcap = ath_price_native * Decimal(1e9)
        marketcap_usd = marketcap_native_raw * quote_price_usd if quote_price_usd > 0 else Decimal(0)

        mini_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=3600, max_buckets=24)
        series_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=chartres, max_buckets=None) if series else []

        holders_list: list[dict[str, Any]] = []

        with db_cursor() as cur:
            cur.execute(
                f"""
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
                FROM launchpad_positions_live
                WHERE token = %s AND balance_token > 0 AND user_address <> ALL(%s)
                  AND {_sql_not_internal("user_address")}
                ORDER BY balance_token DESC
                LIMIT 50
                """,
                (token_addr, excluded_static),
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

            if quote_price_usd > 0:
                balance_usd = current_value_native * quote_price_usd / _WEI
                total_pnl_usd = total_pnl * quote_price_usd / _WEI
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

        top_traders_list: list[dict[str, Any]] = []

        with db_cursor() as cur:
            cur.execute(
                f"""
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
                FROM launchpad_positions_live
                WHERE token = %s AND user_address <> ALL(%s) AND balance_token >= 0
                  AND {_sql_not_internal("user_address")}
                ORDER BY total_pnl_native DESC
                LIMIT 50
                """,
                (token_addr, excluded_static),
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

            if quote_price_usd > 0:
                balance_usd = current_value_native * quote_price_usd / _WEI
                total_pnl_usd = total_pnl * quote_price_usd / _WEI
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
                -- several trades share one block, and one transaction can carry
                -- several of them, so log_index is what puts them in chain order.
                -- without it equal timestamps come back arbitrarily ordered, and
                -- the LIMIT can even return a different set between calls
                ORDER BY timestamp DESC, log_index DESC, txhash DESC
                LIMIT 50
                """,
                (token_addr,),
            )
            trade_rows = cur.fetchall()

        recent_trades_raw = trade_rows
        trades_out: list[dict[str, Any]] = []

        for (
            log_index,
            ts_tr,
            user_address,
            is_buy,
            native_amount,
            token_amount,
            price_native,
            txhash,
        ) in recent_trades_raw:
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
                        "priceNativePerTokenWad": _scaled_price(price_native),
                    }
                }
            )

        tracked_addrs: set[str] = set()
        if tracked:
            for part in tracked.split(","):
                a = part.strip().lower()
                if a:
                    tracked_addrs.add(a)

        tracked_trades_out: list[dict[str, Any]] = []
        TRACKED_HISTORY_LIMIT = 500
        tracked_rows: list = []
        if tracked_addrs:
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
                    WHERE token = %s AND user_address = ANY(%s)
                    ORDER BY timestamp DESC, log_index DESC, txhash DESC
                    LIMIT %s
                    """,
                    (token_addr, sorted(tracked_addrs), TRACKED_HISTORY_LIMIT),
                )
                tracked_rows = cur.fetchall()

        if tracked_rows:
            for (
                log_index,
                ts_tr,
                user_address,
                is_buy,
                native_amount,
                token_amount,
                price_native,
                txhash,
            ) in tracked_rows:
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
                            "priceNativePerTokenWad": _scaled_price(price_native),
                        }
                    }
                )
                if len(tracked_trades_out) >= TRACKED_HISTORY_LIMIT:
                    break

        if trade_rows:
            last_timestamp = int(trade_rows[0][1])
        else:
            last_timestamp = int(created_at or 0) or int(time.time())

        description_val = description or ""
        metadata_cid_val = metadata_cid or ""

        migrated_flag = bool(migrated)

        _res = {
            "migrated": migrated_flag,
            "market": (core.get("market") or "").lower(),
            "reserveQuote": str(int(core.get("curve_native_reserve") or 0)),
            "reserveBase": str(int(core.get("curve_token_reserve") or 0)),
        }
        _apply_live_pool_reserves({token_addr: _res})
        overview_reserves = {
            "reserveQuote": _res["reserveQuote"],
            "reserveBase": _res["reserveBase"],
            "reservesFrom": _res.get("reservesFrom", "curve"),
            "reservesSyncedAt": _res.get("reservesSyncedAt", 0),
        }

        dev_tokens_list: list[dict[str, Any]] = []
        dev_tokens_total = 0
        if creator:
            cutoff_ts = now_ts - 3600

            with db_cursor() as cur:
                cur.execute(
                    """
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
                """,
                    (cutoff_ts, creator),
                )
                dev_token_rows = cur.fetchall()
                dev_tokens_total = len(dev_token_rows)

            for row in dev_token_rows:
                dev_last_price = row[4] or Decimal(0)
                dev_price_wad = dev_last_price * Decimal(1e9)
                dev_tokens_list.append(
                    {
                        "id": row[0],
                        "name": row[1],
                        "symbol": row[2],
                        "metadataCID": row[3] or "",
                        "imageUrl": row[3] or "",
                        "lastPriceNativePerTokenWad": str(dev_price_wad),
                        "marketcap": str(dev_price_wad),
                        "migrated": bool(row[5]),
                        "volumeNative1h": str(int(row[9] or 0)),
                        "holders": int(row[10] or 0),
                        "timestamp": str(int(row[6] or 0)),
                        "market": row[7] or None,
                        "source": _api_source(row[8]),
                    }
                )

        graduation_bps = _graduation_pct(circulating_supply, source)

        sniper_addresses: list[str] = []
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

        sniper_share = float(Decimal(sniper_balance) / _PCT_OF_SUPPLY) if sniper_balance > 0 else 0.0

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
            "lastPriceNativePerTokenWad": _scaled_price(last_price_native),
            "lastUpdatedAt": str(last_timestamp),
            "market": market,
            "marketcap": _fmt(marketcap_native_raw),
            "marketcap_usd": _fmt_usd(marketcap_usd),
            "athPriceNative": _fmt(ath_price_native),
            "athMarketcap": _fmt(ath_marketcap),
            "athMarketcapUsd": _fmt_usd(ath_marketcap * quote_price_usd) if quote_price_usd > 0 else "0",
            "metadataCID": metadata_cid_val,
            "imageUrl": metadata_cid_val,
            "migrated": migrated_flag,
            "migratedAt": migrated_at,
            "mini": {
                "klines": mini_klines,
            },
            "name": name,
            "sellTxs": sells_24h,
            "series": {
                "klines": series_klines,
            },
            "snipers": snipers_view,
            "stats": token_stats(token_addr),
            "fees": {
                "curveFeeRate": _fmt(_nadfun_geo.fee_rate_for(source))
                if _nadfun_geo.is_nadfun_source(source)
                else None,
                "pair": storage.get_pair_fees((market or "").lower()) if market and source != 0 else None,
                "crystalMarket": (
                    {"market": (market or "").lower(), "takerFee": tf}
                    if source == 0
                    and market
                    and (tf := storage.get_taker_fees_batch([market]).get((market or "").lower()))
                    else None
                ),
            },
            "sourceRaw": int(source or 0),
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
            "volume24hUsd": _fmt_usd(volume_usd_24h),
            "volumeLifetimeNative": str(volume_native_lifetime),
            "volumeLifetimeUsd": _fmt_usd(volume_usd_lifetime),
            "feesLifetimeUsd": _fmt_usd(fees_usd_lifetime),
            "graduationPercentageBps": graduation_bps,
            "circulating_supply": str(int(circulating_supply or 0)),
            **_lifecycle_fields(
                source=source,
                circulating_supply=circulating_supply,
                tx_count=int(core.get("tx_count") or 0),
                migrated=migrated_flag,
            ),
            "source": _api_source(source),
            "nadfunVersion": _nadfun_version(token_addr, source),
            "quoteToken": quote_token,
            **overview_reserves,
        }

        return result
    except Exception:
        print(f"[token_overview_graph] error token={token_addr}")
        traceback.print_exc()
        raise
    finally:
        dt = (time.time() - t0) * 1000
        log.info("token_overview_graph token=%s chartres=%s dt_ms=%.1f", token_addr, chartres, dt)


@router.get("/user")
def users_portfolio_batch(
    addresses: str = Query("", description="comma separated wallet addresses, max 25 (100 when merged)"),
    token: str = Query("", description="optional, restrict positions to one token"),
    merged: bool = False,
    limit: int = 0,
    offset: int = 0,
) -> dict[str, Any]:
    addrs: list[str] = []
    for a in (addresses or "").split(","):
        a = a.strip().lower()
        if a and a not in addrs:
            addrs.append(a)
    if not addrs:
        return {"users": {}, "count": 0}
    if len(addrs) > (100 if merged else 25):
        raise HTTPException(status_code=400, detail="max 100 addresses merged, 25 otherwise")

    tok = (token or "").strip().lower()

    if merged:
        return {
            "merged": _merged_portfolio(addrs, tok or None, limit=max(0, min(limit, 1000)), offset=max(0, offset)),
            "addresses": addrs,
            "count": len(addrs),
            "token": tok or None,
        }

    out: dict[str, Any] = {}
    for a in addrs:
        body = user_portfolio(a)
        if tok:
            body = dict(body)
            body["positions"] = [p for p in body.get("positions", []) if (p.get("token") or "").lower() == tok]
        out[a] = body

    return {"users": out, "count": len(out), "token": tok or None}


def _merged_portfolio(addrs: list[str], tok: str | None, limit: int = 0, offset: int = 0) -> dict[str, Any]:
    mon_price = _mon_price_usd()

    where = "p.user_address = ANY(%s)"
    params: tuple = (addrs,)
    if tok:
        where += " AND p.token = %s"
        params = (addrs, tok)

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                p.token,
                SUM(p.token_bought),
                SUM(p.token_sold),
                SUM(p.native_spent),
                SUM(p.native_received),
                SUM(p.balance_token),
                SUM(p.realized_pnl_native),
                SUM(p.unrealized_pnl_native),
                SUM(p.total_pnl_native),
                SUM(p.trade_count),
                SUM(p.buy_count),
                SUM(p.sell_count),
                COUNT(*),
                t.name,
                t.symbol,
                t.metadata_cid,
                t.last_price_native,
                t.market,
                t.source
            FROM launchpad_positions_live p
            JOIN launchpad_tokens t ON t.token = p.token
            WHERE {where}
            GROUP BY p.token, t.name, t.symbol, t.metadata_cid, t.last_price_native, t.market, t.source
            """,
            params,
        )
        rows = cur.fetchall()

    positions: list[dict[str, Any]] = []
    total_value_native = Decimal(0)
    total_realized_pnl = Decimal(0)
    total_unrealized_pnl = Decimal(0)
    total_native_spent = 0
    total_native_received = 0
    total_trades = 0

    for (
        token,
        token_bought,
        token_sold,
        native_spent,
        native_received,
        balance_token,
        realized_pnl,
        unrealized_pnl,
        total_pnl,
        trade_count,
        buy_count,
        sell_count,
        wallet_count,
        name,
        symbol,
        metadata_cid,
        last_price_native,
        market,
        source,
    ) in rows:
        last_price_native = last_price_native or Decimal(0)
        balance_token = int(balance_token or 0)
        realized_pnl = realized_pnl or Decimal(0)
        unrealized_pnl = unrealized_pnl or Decimal(0)
        total_pnl = total_pnl or (realized_pnl + unrealized_pnl)
        current_value_native = Decimal(balance_token) * last_price_native

        total_value_native += current_value_native
        total_realized_pnl += realized_pnl
        total_unrealized_pnl += unrealized_pnl
        total_native_spent += int(native_spent or 0)
        total_native_received += int(native_received or 0)
        total_trades += int(trade_count or 0)

        current_value_usd = current_value_native * mon_price / _WEI if mon_price > 0 else Decimal(0)
        total_pnl_usd = total_pnl * mon_price / _WEI if mon_price > 0 else Decimal(0)

        positions.append(
            {
                "token": token,
                "symbol": symbol,
                "name": name,
                "metadata_cid": metadata_cid or "",
                "balance_token": str(balance_token),
                "balance_native": _fmt(current_value_native),
                "balance_usd": _fmt_usd(current_value_usd),
                "native_spent": str(int(native_spent or 0)),
                "native_received": str(int(native_received or 0)),
                "realized_pnl_native": _fmt(realized_pnl),
                "unrealized_pnl_native": _fmt(unrealized_pnl),
                "total_pnl_native": _fmt(total_pnl),
                "total_pnl_usd": _fmt_usd(total_pnl_usd),
                "last_price_native": _fmt(last_price_native),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
                "token_bought": str(int(token_bought or 0)),
                "token_sold": str(int(token_sold or 0)),
                "wallet_count": int(wallet_count or 0),
                "market": market or None,
                "source": _api_source(source),
            }
        )

    positions.sort(
        key=lambda p: Decimal(p["total_pnl_native"]) if p["total_pnl_native"] is not None else Decimal(0),
        reverse=True,
    )

    total_pnl_native_val = total_realized_pnl + total_unrealized_pnl
    total_value_usd = total_value_native * mon_price / _WEI if mon_price > 0 else Decimal(0)
    total_pnl_usd = total_pnl_native_val * mon_price / _WEI if mon_price > 0 else Decimal(0)

    summary = {
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

    total = len(positions)
    if limit > 0:
        positions = positions[max(0, offset) : max(0, offset) + limit]

    return {"summary": summary, "positions": positions, "total": total, "limit": limit, "offset": offset}


@router.get("/user/{user_addr}")
def user_portfolio(user_addr: str, include_native: bool = False) -> dict[str, Any]:
    user_addr = user_addr.lower()
    mon_price = _mon_price_usd()

    positions: list[dict[str, Any]] = []

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
            FROM launchpad_positions_live p
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
            current_value_usd = current_value_native * mon_price / _WEI
            total_pnl_usd = total_pnl * mon_price / _WEI
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
                "last_price_native": _fmt(last_price_native),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
                "token_bought": str(token_bought),
                "token_sold": str(token_sold),
                "market": market or None,
                "source": _api_source(source),
            }
        )

    positions.sort(
        key=lambda p: Decimal(p["total_pnl_native"]) if p["total_pnl_native"] is not None else Decimal(0),
        reverse=True,
    )

    if mon_price > 0:
        total_value_usd = total_value_native * mon_price / _WEI
        total_pnl_native_val = total_realized_pnl + total_unrealized_pnl
        total_pnl_usd = total_pnl_native_val * mon_price / _WEI
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

    out = {
        "user": user_addr,
        "summary": summary,
        "positions": positions,
    }

    if include_native:
        from api.spot_data import fetch_native_balance

        try:
            native_block, native_raw, native_stale = fetch_native_balance(user_addr)
            out["native_balance"] = str(native_raw)
            out["native_balance_block"] = native_block
            out["native_stale"] = native_stale
        except Exception:
            out["native_balance"] = None
            out["native_balance_block"] = None
            out["native_stale"] = True

    return out


@router.get("/portfolio/{address}")
def portfolio_summary(address: str) -> dict[str, Any]:
    user_addr = address.lower()

    mon_price = _mon_price_usd()

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                SUM(p.balance_token * COALESCE(t.last_price_native, 0)) as portfolio_value,
                SUM(p.realized_pnl_native) as realized_pnl,
                SUM(p.unrealized_pnl_native) as unrealized_pnl,
                SUM(p.native_spent) as native_spent,
                SUM(p.native_received) as native_received,
                SUM(p.trade_count) as trade_count,
                COUNT(*) FILTER (WHERE p.balance_token > 0) as active_positions,
                COUNT(*) FILTER (WHERE p.trade_count > 0) as tokens_traded
            FROM launchpad_positions_live p
            LEFT JOIN launchpad_tokens t ON t.token = p.token
            WHERE p.user_address = %s
            """,
            (user_addr,),
        )
        row = cur.fetchone()

    portfolio_value = row[0] or Decimal(0)
    realized_pnl = row[1] or Decimal(0)
    unrealized_pnl = row[2] or Decimal(0)
    native_spent = int(row[3] or 0)
    native_received = int(row[4] or 0)
    trade_count = int(row[5] or 0)
    active_positions = int(row[6] or 0)
    tokens_traded = int(row[7] or 0)

    total_pnl = realized_pnl + unrealized_pnl

    portfolio_value_usd = portfolio_value * mon_price / _WEI if mon_price > 0 else Decimal(0)
    total_pnl_usd = total_pnl * mon_price / _WEI if mon_price > 0 else Decimal(0)

    return {
        "address": user_addr,
        "summary": {
            "portfolio_value_native": _fmt(portfolio_value),
            "portfolio_value_usd": _fmt_usd(portfolio_value_usd),
            "realized_pnl_native": _fmt(realized_pnl),
            "unrealized_pnl_native": _fmt(unrealized_pnl),
            "total_pnl_native": _fmt(total_pnl),
            "total_pnl_usd": _fmt_usd(total_pnl_usd),
            "native_spent": str(native_spent),
            "native_received": str(native_received),
            "trade_count": trade_count,
            "active_positions": active_positions,
            "tokens_traded": tokens_traded,
        },
    }


@router.get("/portfolio/{address}/positions")
def portfolio_positions(
    address: str,
    sort: str = Query("pnl", description="Sort by: 'pnl' or 'balance'"),
    active_only: bool = Query(True, description="Only positions with balance > 0"),
    cursor: str = Query(None, description="Base64 cursor for pagination"),
    limit: int = Query(20, description="Page size (1-100)"),
) -> dict[str, Any]:
    user_addr = address.lower()

    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit must be between 1 and 100")

    if sort not in ("pnl", "balance"):
        raise HTTPException(400, "sort must be 'pnl' or 'balance'")

    mon_price = _mon_price_usd()

    cursor_v: Decimal | None = None
    cursor_t: str | None = None
    if cursor:
        cursor_v, cursor_t = _parse_positions_cursor(cursor)

    sort_col = "p.total_pnl_native" if sort == "pnl" else "p.balance_token"

    if active_only:
        base_where = "p.user_address = %s AND p.balance_token > 0"
    else:
        base_where = "p.user_address = %s"

    if cursor_v is not None and cursor_t is not None:
        where_clause = f"{base_where} AND ({sort_col}, p.token) < (%s, %s)"
        params: tuple = (user_addr, cursor_v, cursor_t) if active_only else (user_addr, cursor_v, cursor_t)
    else:
        where_clause = base_where
        params = (user_addr,)

    query = f"""
        SELECT p.token, p.balance_token, p.native_spent, p.native_received,
               p.token_bought, p.token_sold, p.realized_pnl_native,
               p.unrealized_pnl_native, p.total_pnl_native, p.trade_count,
               p.buy_count, p.sell_count,
               t.name, t.symbol, t.metadata_cid, t.last_price_native, t.market, t.source
        FROM launchpad_positions_live p
        JOIN launchpad_tokens t ON t.token = p.token
        WHERE {where_clause}
        ORDER BY {sort_col} DESC, p.token DESC
        LIMIT %s
    """

    with db_cursor() as cur:
        cur.execute(query, params + (limit + 1,))
        rows = cur.fetchall()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    positions = []
    for row in rows:
        (
            token,
            balance_token,
            native_spent,
            native_received,
            token_bought,
            token_sold,
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
        ) = row

        last_price_native = last_price_native or Decimal(0)
        balance_token = int(balance_token or 0)
        native_spent = int(native_spent or 0)
        native_received = int(native_received or 0)
        token_bought = int(token_bought or 0)
        token_sold = int(token_sold or 0)
        realized_pnl = realized_pnl_native or Decimal(0)
        unrealized_pnl = unrealized_pnl_native or Decimal(0)
        total_pnl = total_pnl_native or (realized_pnl + unrealized_pnl)

        current_value_native = Decimal(balance_token) * last_price_native
        current_value_usd = current_value_native * mon_price / _WEI if mon_price > 0 else Decimal(0)
        total_pnl_usd = total_pnl * mon_price / _WEI if mon_price > 0 else Decimal(0)

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
                "token_bought": str(token_bought),
                "token_sold": str(token_sold),
                "realized_pnl_native": _fmt(realized_pnl),
                "unrealized_pnl_native": _fmt(unrealized_pnl),
                "total_pnl_native": _fmt(total_pnl),
                "total_pnl_usd": _fmt_usd(total_pnl_usd),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
                "market": market,
                "source": _api_source(source),
            }
        )

    next_cursor = None
    if has_more and positions:
        last_row = rows[-1]
        if sort == "pnl":
            cursor_val = last_row[8]
        else:
            cursor_val = last_row[1]
        next_cursor = _encode_cursor(
            {
                "v": str(cursor_val),
                "t": last_row[0],
            }
        )

    return {
        "positions": positions,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/portfolio/{address}/history")
def portfolio_history(
    address: str,
    token: str = Query(None, description="Filter to specific token"),
    cursor: str = Query(None, description="Base64 cursor for pagination"),
    limit: int = Query(50, description="Page size (1-200)"),
) -> dict[str, Any]:
    user_addr = address.lower()

    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200")

    cursor_ts: int | None = None
    cursor_li: int | None = None
    cursor_tx: str | None = None
    if cursor:
        cursor_ts, cursor_li, cursor_tx = _parse_history_cursor(cursor)

    if token:
        token = token.lower()
        if cursor_ts is not None:
            where_clause = (
                "tr.user_address = %s AND tr.token = %s AND (tr.timestamp, tr.log_index, tr.txhash) < (%s, %s, %s)"
            )
            params: tuple = (user_addr, token, cursor_ts, cursor_li, cursor_tx)
        else:
            where_clause = "tr.user_address = %s AND tr.token = %s"
            params = (user_addr, token)
    else:
        if cursor_ts is not None:
            where_clause = "tr.user_address = %s AND (tr.timestamp, tr.log_index, tr.txhash) < (%s, %s, %s)"
            params = (user_addr, cursor_ts, cursor_li, cursor_tx)
        else:
            where_clause = "tr.user_address = %s"
            params = (user_addr,)

    query = f"""
        SELECT tr.txhash, tr.timestamp, tr.log_index, tr.token, tr.is_buy,
               tr.native_amount, tr.token_amount, tr.price_native, tr.usd_amount,
               t.symbol, t.name
        FROM launchpad_trades tr
        JOIN launchpad_tokens t ON t.token = tr.token
        WHERE {where_clause}
        ORDER BY tr.timestamp DESC, tr.log_index DESC, tr.txhash DESC
        LIMIT %s
    """

    with db_cursor() as cur:
        cur.execute(query, params + (limit + 1,))
        rows = cur.fetchall()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    trades = []
    for row in rows:
        (
            txhash,
            timestamp,
            log_index,
            trade_token,
            is_buy,
            native_amount,
            token_amount,
            price_native,
            usd_amount,
            symbol,
            name,
        ) = row

        native_amount = int(native_amount or 0)
        token_amount = int(token_amount or 0)
        usd_amount_dec = usd_amount or Decimal(0)

        trades.append(
            {
                "txhash": txhash,
                "timestamp": int(timestamp),
                "log_index": int(log_index),
                "token": trade_token,
                "symbol": symbol,
                "name": name,
                "is_buy": bool(is_buy),
                "native_amount": str(native_amount),
                "token_amount": str(token_amount),
                "price_native": _fmt(price_native or Decimal(0)),
                "usd_amount": _fmt_usd(usd_amount_dec),
            }
        )

    next_cursor = None
    if has_more and trades:
        last_row = rows[-1]
        next_cursor = _encode_cursor(
            {
                "ts": int(last_row[1]),
                "li": int(last_row[2]),
                "tx": last_row[0],
            }
        )

    return {
        "trades": trades,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _safe_timezone(tz: str) -> str:
    name = (tz or "UTC").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return name


@router.get("/portfolio/{address}/daily")
def portfolio_daily(
    address: str,
    days: int = Query(90, ge=1, le=365, description="trailing days to return"),
    tz: str = "UTC",
) -> dict[str, Any]:
    user_addr = address.lower()
    zone = _safe_timezone(tz)
    since_day = datetime.now(ZoneInfo(zone)).date() - timedelta(days=days - 1)

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT (to_timestamp(timestamp) AT TIME ZONE %s)::date AS day,
                   SUM(realized_native) AS realized,
                   SUM(native_amount::numeric) AS volume_native,
                   SUM(COALESCE(usd_amount, 0)) AS volume_usd,
                   SUM(CASE WHEN is_buy THEN native_amount::numeric ELSE 0 END) AS buy_native,
                   SUM(CASE WHEN NOT is_buy THEN native_amount::numeric ELSE 0 END) AS sell_native,
                   COUNT(*) AS trade_count,
                   COUNT(*) FILTER (WHERE is_buy) AS buy_count,
                   COUNT(*) FILTER (WHERE NOT is_buy) AS sell_count
            FROM launchpad_trades
            WHERE user_address = %s
            GROUP BY 1
            HAVING (to_timestamp(timestamp) AT TIME ZONE %s)::date >= %s
            ORDER BY 1
            """,
            (zone, user_addr, zone, since_day),
        )
        rows = cur.fetchall()

    out_rows = []
    for day, realized, vol_native, vol_usd, buy_native, sell_native, trade_count, buy_count, sell_count in rows:
        out_rows.append(
            {
                "day": day.isoformat(),
                "realized_pnl_native": _fmt(realized or Decimal(0)),
                "volume_native": str(int(vol_native or 0)),
                "volume_usd": _fmt_usd(vol_usd or Decimal(0)),
                "buy_volume_native": str(int(buy_native or 0)),
                "sell_volume_native": str(int(sell_native or 0)),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
            }
        )

    return {
        "user": user_addr,
        "days": days,
        "rows": out_rows,
        "as_of_block": storage.get_last_processed_block() or 0,
    }


@router.get("/leaderboard")
def leaderboard(
    search: str = Query(None, description="Filter by address prefix"),
    limit: int = Query(100, description="Max results (1-100)"),
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit must be between 1 and 100")

    if search:
        search = search.lower()
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT address, total_realized_pnl_native, total_native_volume,
                       total_trades, tokens_created, tokens_graduated
                FROM launchpad_users
                WHERE address ILIKE %s
                ORDER BY address
                LIMIT %s
                """,
                (search + "%", limit),
            )
            rows = cur.fetchall()

        users = []
        for i, row in enumerate(rows):
            users.append(
                {
                    "rank": i + 1,
                    "address": row[0],
                    "total_realized_pnl_native": _fmt(row[1] or Decimal(0)),
                    "total_native_volume": _fmt(row[2] or Decimal(0)),
                    "total_trades": int(row[3] or 0),
                    "tokens_created": int(row[4] or 0),
                    "tokens_graduated": int(row[5] or 0),
                }
            )

        return {"users": users}

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT address, total_realized_pnl_native, total_native_volume,
                   total_trades, tokens_created, tokens_graduated
            FROM launchpad_users
            WHERE total_realized_pnl_native > 0
            ORDER BY total_realized_pnl_native DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    users = []
    for i, row in enumerate(rows):
        users.append(
            {
                "rank": i + 1,
                "address": row[0],
                "total_realized_pnl_native": _fmt(row[1] or Decimal(0)),
                "total_native_volume": _fmt(row[2] or Decimal(0)),
                "total_trades": int(row[3] or 0),
                "tokens_created": int(row[4] or 0),
                "tokens_graduated": int(row[5] or 0),
            }
        )

    return {"users": users}


@router.get("/stats/{token_addr}")
@ttl_cache("stats:token", ttl_seconds=0.5)
def token_stats(token_addr: str) -> dict[str, Any]:
    token_addr = token_addr.lower()

    now_ts = int(time.time())
    bounds = {
        "5m": now_ts - 5 * 60,
        "1h": now_ts - 60 * 60,
        "6h": now_ts - 6 * 60 * 60,
        "24h": now_ts - 24 * 60 * 60,
    }
    t5m, t1h, t6h, t24h = bounds["5m"], bounds["1h"], bounds["6h"], bounds["24h"]

    out: dict[str, Any] = {
        "type": "stats",
        "token": token_addr,
    }

    agg_sql = (
        """
        SELECT
    """
        + ",\n".join(
            f"""
            COALESCE(SUM(usd_amount) FILTER (WHERE timestamp > %(t{k})s), 0),
            COALESCE(SUM(usd_amount) FILTER (WHERE timestamp > %(t{k})s AND is_buy), 0),
            COALESCE(SUM(usd_amount) FILTER (WHERE timestamp > %(t{k})s AND NOT is_buy), 0),
            COUNT(*) FILTER (WHERE timestamp > %(t{k})s AND is_buy),
            COUNT(*) FILTER (WHERE timestamp > %(t{k})s AND NOT is_buy)
        """
            for k in ("5m", "1h", "6h", "24h")
        )
        + """
        FROM launchpad_trades
        WHERE token = %(tok)s AND timestamp > %(t24h)s AND timestamp <= %(now)s
    """
    )

    price_sql = """
        SELECT
            (SELECT price_native FROM launchpad_trades
              WHERE token = %(tok)s
              ORDER BY timestamp DESC, log_index DESC LIMIT 1),
            (SELECT price_native FROM launchpad_trades
              WHERE token = %(tok)s
              ORDER BY timestamp ASC, log_index ASC LIMIT 1),
            (SELECT timestamp FROM launchpad_trades
              WHERE token = %(tok)s
              ORDER BY timestamp DESC, log_index DESC LIMIT 1),
            (SELECT MAX(number) FROM launchpad_blocks),
            (SELECT price_native FROM launchpad_trades
              WHERE token = %(tok)s AND timestamp <= %(t5m)s
              ORDER BY timestamp DESC, log_index DESC LIMIT 1),
            (SELECT price_native FROM launchpad_trades
              WHERE token = %(tok)s AND timestamp <= %(t1h)s
              ORDER BY timestamp DESC, log_index DESC LIMIT 1),
            (SELECT price_native FROM launchpad_trades
              WHERE token = %(tok)s AND timestamp <= %(t6h)s
              ORDER BY timestamp DESC, log_index DESC LIMIT 1),
            (SELECT price_native FROM launchpad_trades
              WHERE token = %(tok)s AND timestamp <= %(t24h)s
              ORDER BY timestamp DESC, log_index DESC LIMIT 1)
    """

    params = {
        "tok": token_addr,
        "now": now_ts,
        "t5m": t5m,
        "t1h": t1h,
        "t6h": t6h,
        "t24h": t24h,
    }

    with db_cursor() as cur:
        cur.execute(agg_sql, params)
        agg = cur.fetchone() or (0,) * 20
        cur.execute(price_sql, params)
        prices = cur.fetchone() or (None,) * 8

    latest_price = prices[0] or None
    first_price = prices[1] or None
    out["as_of_ts"] = int(prices[2] or 0)
    out["as_of_block"] = int(prices[3] or 0)
    refs = {
        "5m": prices[4] or None,
        "1h": prices[5] or None,
        "6h": prices[6] or None,
        "24h": prices[7] or None,
    }

    for i, suffix in enumerate(("5m", "1h", "6h", "24h")):
        base = i * 5
        volume_usd = agg[base] or Decimal(0)
        buy_volume_usd = agg[base + 1] or Decimal(0)
        sell_volume_usd = agg[base + 2] or Decimal(0)
        buy_tx_count = agg[base + 3] or 0
        sell_tx_count = agg[base + 4] or 0

        base_price = refs[suffix] or first_price
        last_eff = latest_price or first_price

        if not base_price or last_eff is None:
            change_pct = 0.0
        else:
            change_pct = float((last_eff - base_price) / base_price * Decimal(100))

        out[f"volume_usd_{suffix}"] = float(volume_usd)
        out[f"buy_volume_usd_{suffix}"] = float(buy_volume_usd)
        out[f"sell_volume_usd_{suffix}"] = float(sell_volume_usd)
        out[f"buy_tx_count_{suffix}"] = int(buy_tx_count)
        out[f"sell_tx_count_{suffix}"] = int(sell_tx_count)
        out[f"change_pct_{suffix}"] = change_pct
        out[f"price_ref_{suffix}"] = _scaled_price(base_price) if base_price else "0"

    return out


@router.get("/trades/{addresses}")
def trades_for_addresses(addresses: str) -> dict[str, Any]:
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
            -- log_index keeps same block trades in chain order, see above
            ORDER BY timestamp DESC, log_index DESC, txhash DESC
            LIMIT 50
            """,
            (list(addrs),),
        )
        rows = cur.fetchall()

    out: list[dict[str, Any]] = []

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
                    "priceNativePerTokenWad": _scaled_price(price_native),
                }
            }
        )

    return {
        "addresses": list(addrs),
        "count": len(out),
        "trades": out,
    }


@router.get("/chart/{token_addr}/{chartres}")
def chart_only(
    token_addr: str,
    chartres: int,
    before_ts: int = 0,
    limit: int = 0,
    rates: int = 0,
) -> dict[str, Any]:
    token_addr = token_addr.lower()

    if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
        raise HTTPException(status_code=400)

    lim = max(1, min(int(limit or 1000), 5000))

    out = _build_ohlcv_from_db(
        token_addr,
        bucket_seconds=chartres,
        max_buckets=lim,
        before_ts=int(before_ts) or None,
    )

    mon_usd = None
    if rates and out:
        from api.routes.orderbook import coarsened_resolution
        from api.spot_graph import _MIN_PRICE_TRADE_WEI, _mon_usd_at

        r_start = int(out[0]["time"])
        r_end = int(out[-1]["time"]) + chartres
        r_res = coarsened_resolution(r_end - r_start, max(60, chartres))
        points = storage.mon_usd_series(r_start, r_end, r_res, _MIN_PRICE_TRADE_WEI)
        mon_usd = {
            "resolution": r_res,
            "from": r_start,
            "to": r_end,
            "before": float(_mon_usd_at(r_start) or 0) or None,
            "points": [{"t": t, "rate": r} for t, r in points],
        }

    return {
        "token": token_addr,
        "resolution": chartres,
        "before_ts": int(before_ts) or None,
        "next_before_ts": int(out[0]["time"]) if out else None,
        "monUsd": mon_usd,
        "klines": out,
    }


@router.get("/spot/{wallet}")
def spot_portfolio(
    wallet: str,
    all: bool = Query(False, description="include zero balances"),
    addresses: str = "",
) -> dict[str, Any]:
    from api.spot_data import spot_body
    from api.spot_graph import RESOLUTION, ensure_fill, graph_for

    wallet = wallet.lower()
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise HTTPException(status_code=400, detail="invalid wallet address")

    selected = []
    for a in (addresses or "").split(","):
        a = a.strip().lower()
        if a.startswith("0x") and len(a) == 42 and a not in selected:
            selected.append(a)
    if len(selected) > 16:
        raise HTTPException(status_code=400, detail="at most 16 addresses")

    try:
        body = spot_body(selected or wallet, include_zero=all)
    except Exception:
        raise HTTPException(status_code=503, detail="balance read failed and no cached snapshot exists")

    if body["supported"]:
        ensure_fill(wallet)
        graph = graph_for(wallet)
    else:
        graph = {"resolution": RESOLUTION, "points": [], "complete": True}

    return {
        **body,
        "graph": graph,
        "as_of_block": storage.get_last_processed_block() or 0,
    }


@router.get("/volume/{user_addr}")
def user_volume(user_addr: str) -> dict[str, Any]:
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

    with db_cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(usd_amount), 0) FROM launchpad_trades WHERE user_address = %s",
            (user_addr,),
        )
        total_usd_volume = cur.fetchone()[0] or Decimal(0)

    total_native_volume_dec = Decimal(total_native_volume)

    return {
        "user": user_addr,
        "volume_native": str(total_native_volume_dec),
        "volume_usd": _fmt_usd(total_usd_volume),
        "trade_count": int(total_trades),
        "tokens_traded": len(seen_tokens),
    }


@router.get("/search/query")
@ttl_cache("tokens:search", ttl_seconds=3)
def search_tokens_api(
    query: str = Query("", max_length=64, description="optional, name symbol or address"),
    sort: str = Query("", description="mc | volume_1h | volume_24h | recent | holders"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    phase: str = Query("", description="new | graduating | graduated"),
    age_min: float | None = Query(None, description="minutes since creation"),
    age_max: float | None = Query(None),
    holders_min: float | None = Query(None),
    holders_max: float | None = Query(None),
    top10_min: float | None = Query(None, description="percent of supply"),
    top10_max: float | None = Query(None),
    dev_holding_min: float | None = Query(None, description="percent of supply"),
    dev_holding_max: float | None = Query(None),
    source: str = Query("", description="0 native/crystal, 1 nad.fun"),
    exclude_dev: str = Query("", description="comma separated creator addresses"),
    exclude_ca: str = Query("", description="comma separated token addresses"),
    exclude_website: str = Query("", description="comma separated bare hostnames"),
    exclude_twitter: str = Query("", description="comma separated bare handles"),
    sniper_holding_min: float | None = Query(None, description="percent of supply"),
    sniper_holding_max: float | None = Query(None),
    pro_traders_min: float | None = Query(None, description="wallets profitable over >=10 trades"),
    pro_traders_max: float | None = Query(None),
    insider_holding_min: float | None = Query(None, description="percent of supply held above net bought"),
    insider_holding_max: float | None = Query(None),
    marketcap_min: float | None = Query(None, description="usd"),
    marketcap_max: float | None = Query(None),
    volume_24h_min: float | None = Query(None, description="usd"),
    volume_24h_max: float | None = Query(None),
    fees_min: float | None = Query(None, description="usd"),
    fees_max: float | None = Query(None),
    buy_tx_min: float | None = Query(None),
    buy_tx_max: float | None = Query(None),
    sell_tx_min: float | None = Query(None),
    sell_tx_max: float | None = Query(None),
    price_min: float | None = Query(None),
    price_max: float | None = Query(None),
    keywords: str = Query("", description="comma separated, matched on name and symbol"),
    exclude_keywords: str = Query("", description="comma separated, excluded on match"),
    has_website: bool = Query(False),
    has_twitter: bool = Query(False),
    has_telegram: bool = Query(False),
    has_discord: bool = Query(False),
) -> dict[str, Any]:
    filters = {
        "phase": phase,
        "age_min": age_min,
        "age_max": age_max,
        "holders_min": holders_min,
        "holders_max": holders_max,
        "top10_min": top10_min,
        "top10_max": top10_max,
        "dev_holding_min": dev_holding_min,
        "dev_holding_max": dev_holding_max,
        "source": source or None,
        "exclude_dev": _csv(exclude_dev),
        "exclude_ca": _csv(exclude_ca),
        "exclude_website": _csv(exclude_website),
        "exclude_twitter": _csv(exclude_twitter),
        "sniper_holding_min": sniper_holding_min,
        "sniper_holding_max": sniper_holding_max,
        "pro_traders_min": pro_traders_min,
        "pro_traders_max": pro_traders_max,
        "insider_holding_min": insider_holding_min,
        "insider_holding_max": insider_holding_max,
        "marketcap_min": marketcap_min,
        "marketcap_max": marketcap_max,
        "volume_24h_min": volume_24h_min,
        "volume_24h_max": volume_24h_max,
        "fees_min": fees_min,
        "fees_max": fees_max,
        "buy_tx_min": buy_tx_min,
        "buy_tx_max": buy_tx_max,
        "sell_tx_min": sell_tx_min,
        "sell_tx_max": sell_tx_max,
        "price_min": price_min,
        "price_max": price_max,
        "keywords": keywords,
        "exclude_keywords": exclude_keywords,
        "has_website": has_website,
        "has_twitter": has_twitter,
        "has_telegram": has_telegram,
        "has_discord": has_discord,
    }

    return _search_impl(query, sort, limit, offset, filters)


@router.post("/search/query")
def search_tokens_post(body: dict[str, Any]) -> dict[str, Any]:
    b = body or {}

    def lst(key):
        v = b.get(key)
        if isinstance(v, list):
            return [str(x).strip().lower() for x in v if str(x).strip()]
        return _csv(str(v or ""))

    filters = {k: v for k, v in b.items() if k not in ("query", "sort", "limit", "offset")}
    for key in ("exclude_dev", "exclude_ca", "exclude_website", "exclude_twitter"):
        filters[key] = lst(key)

    return _search_impl(
        str(b.get("query") or ""),
        str(b.get("sort") or ""),
        int(b.get("limit") or 50),
        int(b.get("offset") or 0),
        filters,
    )


def _search_impl(query: str, sort: str, limit: int, offset: int, filters: dict) -> dict[str, Any]:
    token_addrs, circ_map, total = storage.search_tokens_filtered(
        query=query,
        filters=filters,
        sort=sort,
        limit=limit,
        offset=offset,
        mon_usd=_mon_price_usd(),
    )

    base = {
        "query": query,
        "sort": sort or None,
        "limit": limit,
        "offset": offset,
        "total": total,
        "applied_filters": sorted(
            ([k for k, v in filters.items() if v is not None and v is not False and v != "" and v != []])
            + (["query"] if (query or "").strip() else [])
        ),
    }

    if not token_addrs:
        return {**base, "count": 0, "results": []}

    token_data = _batch_serialize_tokens(token_addrs)
    results = []
    for t in token_addrs:
        data = token_data.get(t)
        if not data:
            continue
        data["graduationPercentageBps"] = _graduation_pct(circ_map.get(t), _true_source(data))
        results.append(data)

    return {**base, "count": len(results), "results": results}
