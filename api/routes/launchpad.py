from __future__ import annotations

import traceback
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from api.api import (
    _batch_serialize_tokens,
    _build_ohlcv_from_db,
    _encode_cursor,
    _fmt,
    _fmt_usd,
    _holders_for_token,
    _internal_addrs,
    _lifecycle_fields,
    _mon_price_usd,
    _nadfun_version,
    _parse_history_cursor,
    _parse_positions_cursor,
    _quote_price_usd,
    _scaled_price,
    db_cursor,
    log,
    storage,
    time,
    ttl_cache,
)

router = APIRouter()

from core.adapters.native import CURVE_SUPPLY as _NATIVE_CURVE_SUPPLY

# single source of truth: the native adapter owns our curve geometry
CRYSTAL_GRADUATION_SUPPLY = _NATIVE_CURVE_SUPPLY // 10**18
NADFUN_GRADUATION_SUPPLY = 793_100_000


# fraction of the curve sold, using each source own curve supply
def _graduation_pct(circulating, source) -> float:
    try:
        denom = CRYSTAL_GRADUATION_SUPPLY if int(source or 0) == 0 else NADFUN_GRADUATION_SUPPLY
        return float(circulating or 0) / denom
    except Exception:
        return 0.0


# grouped token lists for recently created approaching and graduated tokens
@router.get("/tokens")
@ttl_cache("tokens:list", ttl_seconds=3)
def list_tokens() -> dict[str, list[dict[str, Any]]]:
    t0 = time.time()
    excluded = _internal_addrs()
    with db_cursor() as cur:
        cur.execute("""
            SELECT token, circulating_supply
            FROM launchpad_tokens
            WHERE migrated = TRUE
            ORDER BY migrated_at DESC NULLS LAST, migrated_block DESC NULLS LAST
            LIMIT 30
        """)
        grad_rows = cur.fetchall()

        graduated_ids = {t.lower() for (t, _) in grad_rows if t}

        if graduated_ids:
            cur.execute(
                """
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                  AND migrated = FALSE
                  AND token <> ALL(%s)
                ORDER BY circulating_supply DESC
                LIMIT 30
            """,
                (list(graduated_ids),),
            )
        else:
            cur.execute("""
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                  AND migrated = FALSE
                ORDER BY circulating_supply DESC
                LIMIT 30
            """)
        appr_rows = cur.fetchall()

        approaching_ids = {t.lower() for (t, _) in appr_rows if t}
        excluded_ids = graduated_ids | approaching_ids

        if excluded_ids:
            cur.execute(
                """
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE token <> ALL(%s)
                ORDER BY created_at DESC NULLS LAST, created_block DESC NULLS LAST
                LIMIT 30
            """,
                (list(excluded_ids),),
            )
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
    token_data = _batch_serialize_tokens(all_token_addrs, excluded)

    # attach graduation progress to a serialized token
    def with_graduation_pct(token_addr):
        data = token_data.get(token_addr, {})
        if data:
            data["graduationPercentageBps"] = _graduation_pct(circ_map.get(token_addr), data.get("source"))
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


# one query for a token core row, holders, 24h stats and creator counts
def _get_token_core_stats(token_addr: str, day_ago: int, excluded: set[str]) -> dict | None:
    excluded_list = list(excluded)
    with db_cursor() as cur:
        cur.execute(
            """
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
                COALESCE(c.tokens_created, 0), COALESCE(c.tokens_graduated, 0),
                COALESCE(t.quote_token, '0x3bd359c1119da7da1d913d1c4d2b7c461115433a'),
                COALESCE(t.ath_price_native, 0)
            FROM token_data t
            CROSS JOIN holder_stats h
            CROSS JOIN trade_stats_24h s
            CROSS JOIN buyer_seller_counts b
            LEFT JOIN creator_stats c ON true
        """,
            (token_addr, excluded_list, token_addr, token_addr, day_ago, excluded_list, excluded_list, token_addr),
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
    }


# trades in a time range, for chart marks that span more history than the fixed
# window returns, must stay registered above the chartres route because that route
# parses its second segment as an int and would claim trades and 422
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


# token overview, holders, recent trades and chart data in one payload
@router.get("/token/{token_addr}/{chartres}")
def token_overview_graph(
    token_addr: str,
    chartres: int,
    tracked: str = Query(
        "",
        description="comma-separated list of addresses to track for trackedtrades",
    ),
) -> dict[str, Any]:
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
        series_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=chartres, max_buckets=None)

        holders_list: list[dict[str, Any]] = []

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

            if quote_price_usd > 0:
                balance_usd = current_value_native * quote_price_usd
                total_pnl_usd = total_pnl * quote_price_usd
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
                WHERE token = %s AND user_address <> ALL(%s) AND balance_token >= 0
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

            if quote_price_usd > 0:
                balance_usd = current_value_native * quote_price_usd
                total_pnl_usd = total_pnl * quote_price_usd
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

        tracked_trades_out: list[dict[str, Any]] = []
        if tracked_addrs and recent_trades_raw:
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
                        "lastPriceNativePerTokenWad": str(dev_price_wad),
                        "marketcap": str(dev_price_wad),
                        "migrated": bool(row[5]),
                        "volumeNative1h": str(int(row[9] or 0)),
                        "holders": int(row[10] or 0),
                        "timestamp": str(int(row[6] or 0)),
                        "market": row[7] or None,
                        "source": int(row[8] or 0),
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
            "lastPriceQuotePerTokenWad": str(last_price_wad),
            "lastUpdatedAt": str(last_timestamp),
            "market": market,
            "marketcap": marketcap_native_raw,
            "marketcap_quote": marketcap_native_raw,
            "marketcap_usd": marketcap_usd,
            "athPriceNative": _fmt(ath_price_native),
            "athMarketcap": ath_marketcap,
            "athMarketcapUsd": ath_marketcap * quote_price_usd if quote_price_usd > 0 else Decimal(0),
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
            "volumeQuote": str(volume_native_24h),
            "volumeUsd": _fmt_usd(volume_usd_24h),
            "graduationPercentageBps": graduation_bps,
            "circulating_supply": str(int(circulating_supply or 0)),
            **_lifecycle_fields(
                source=source,
                circulating_supply=circulating_supply,
                tx_count=int(core.get("tx_count") or 0),
                migrated=migrated_flag,
            ),
            "source": int(source or 0),
            "nadfunVersion": _nadfun_version(token_addr, source),
            "quoteToken": quote_token,
            "quote_token": quote_token,
        }

        return result
    except Exception:
        print(f"[token_overview_graph] error token={token_addr}")
        traceback.print_exc()
        raise
    finally:
        dt = (time.time() - t0) * 1000
        log.info("token_overview_graph token=%s chartres=%s dt_ms=%.1f", token_addr, chartres, dt)


# legacy user portfolio summary and positions
@router.get("/user/{user_addr}")
def user_portfolio(user_addr: str) -> dict[str, Any]:
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


# aggregated portfolio metrics for one user
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
            FROM launchpad_positions p
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

    portfolio_value_usd = portfolio_value * mon_price if mon_price > 0 else Decimal(0)
    total_pnl_usd = total_pnl * mon_price if mon_price > 0 else Decimal(0)

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


# paginated portfolio positions with sorting and cursor support
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
        FROM launchpad_positions p
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
        current_value_usd = current_value_native * mon_price if mon_price > 0 else Decimal(0)
        total_pnl_usd = total_pnl * mon_price if mon_price > 0 else Decimal(0)

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
                "source": int(source or 0),
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


# paginated trade history for one user
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


# ranked users by pnl volume or winrate
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


# windowed volume counts and change percentages for one token
@router.get("/stats/{token_addr}")
def token_stats(token_addr: str) -> dict[str, Any]:
    token_addr = token_addr.lower()

    windows = {
        "5m": 5 * 60,
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
    }

    out: dict[str, Any] = {
        "type": "stats",
        "token": token_addr,
    }

    now_ts = int(time.time())

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT price_native
            FROM launchpad_trades
            WHERE token = %s
            ORDER BY timestamp ASC, log_index ASC
            LIMIT 1
            """,
            (token_addr,),
        )
        first_row = cur.fetchone()
    first_price = (first_row[0] if first_row else None) or None

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
                ORDER BY timestamp DESC, log_index DESC
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
                ORDER BY timestamp DESC, log_index DESC
                LIMIT 1
                """,
                (token_addr, start_ts, now_ts),
            )
            last_row = cur.fetchone()

        prev_price = (prev_row[0] if prev_row else None) or None
        last_price = (last_row[0] if last_row else None) or None

        last_eff = last_price or prev_price or first_price
        base_price = prev_price or first_price

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
        # same scale as `marketcap` and the chart's open/close, so the client can
        # recompute the delta against a live price without a unit conversion
        out[f"price_ref_{suffix}"] = _scaled_price(base_price) if base_price else "0"

    return out


# recent trades for one or more user addresses
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
            ORDER BY timestamp DESC
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
                    "priceNativePerTokenWad": str(price_native or Decimal(0)),
                }
            }
        )

    return {
        "addresses": list(addrs),
        "count": len(out),
        "trades": out,
    }


# ohlcv bars for one token at the requested resolution
@router.get("/chart/{token_addr}/{chartres}")
def chart_only(
    token_addr: str,
    chartres: int,
) -> dict[str, Any]:
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

    return {
        "token": token_addr,
        "resolution": chartres,
        "klines": out,
    }


# total traded volume and trade count for one user
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

    total_native_volume_dec = Decimal(total_native_volume)

    return {
        "user": user_addr,
        "volume_native": str(total_native_volume_dec),
        "trade_count": int(total_trades),
        "tokens_traded": len(seen_tokens),
    }


# search tokens by name or symbol with optional ranked sorting
@router.get("/search/query")
@ttl_cache("tokens:search", ttl_seconds=3)
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
) -> dict[str, Any]:
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
                data["graduationPercentageBps"] = _graduation_pct(circ_map.get(t), data.get("source"))
                results.append(data)

        return {"query": query, "sort": None, "count": len(results), "results": results}

    rows = storage.search_tokens(q, limit=1000)
    token_addrs = [(token or "").lower() for token, _, _ in rows if token]

    if not token_addrs:
        return {"query": query, "sort": sort, "count": 0, "results": []}

    now_ts = int(time.time())

    with db_cursor() as cur:
        if sort == "mc":
            cur.execute(
                """
                SELECT token, last_price_native, circulating_supply
                FROM launchpad_tokens
                WHERE token = ANY(%s)
                ORDER BY last_price_native DESC NULLS LAST
                LIMIT 50
            """,
                (token_addrs,),
            )
        elif sort == "recent":
            cur.execute(
                """
                SELECT token, last_price_native, circulating_supply
                FROM launchpad_tokens
                WHERE token = ANY(%s)
                ORDER BY created_at DESC NULLS LAST
                LIMIT 50
            """,
                (token_addrs,),
            )
        elif sort == "volume_1h":
            cutoff_1h = now_ts - 3600
            cur.execute(
                """
                SELECT t.token, t.last_price_native, t.circulating_supply
                FROM launchpad_tokens t
                LEFT JOIN (
                    SELECT token, COALESCE(SUM(native_amount), 0) as vol
                    FROM launchpad_trades
                    WHERE token = ANY(%s) AND timestamp >= %s
                    GROUP BY token
                ) tr ON tr.token = t.token
                WHERE t.token = ANY(%s)
                ORDER BY tr.vol DESC NULLS LAST
                LIMIT 50
            """,
                (token_addrs, cutoff_1h, token_addrs),
            )
        elif sort == "volume_24h":
            cutoff_24h = now_ts - 86400
            cur.execute(
                """
                SELECT t.token, t.last_price_native, t.circulating_supply
                FROM launchpad_tokens t
                LEFT JOIN (
                    SELECT token, COALESCE(SUM(native_amount), 0) as vol
                    FROM launchpad_trades
                    WHERE token = ANY(%s) AND timestamp >= %s
                    GROUP BY token
                ) tr ON tr.token = t.token
                WHERE t.token = ANY(%s)
                ORDER BY tr.vol DESC NULLS LAST
                LIMIT 50
            """,
                (token_addrs, cutoff_24h, token_addrs),
            )
        elif sort == "holders":
            cur.execute(
                """
                SELECT t.token, t.last_price_native, t.circulating_supply
                FROM launchpad_tokens t
                LEFT JOIN (
                    SELECT token, COUNT(*) as cnt
                    FROM launchpad_positions
                    WHERE token = ANY(%s) AND balance_token > 1
                    GROUP BY token
                ) p ON p.token = t.token
                WHERE t.token = ANY(%s)
                ORDER BY p.cnt DESC NULLS LAST
                LIMIT 50
            """,
                (token_addrs, token_addrs),
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"invalid sort: {sort}. Use 'mc', 'volume_1h', 'volume_24h', 'recent', or 'holders'",
            )

        sorted_rows = cur.fetchall()

    sorted_addrs = [row[0].lower() for row in sorted_rows if row[0]]
    circ_map = {row[0].lower(): row[2] for row in sorted_rows if row[0]}
    token_data = _batch_serialize_tokens(sorted_addrs, excluded)

    results = []
    for t in sorted_addrs:
        if t in token_data:
            data = token_data[t]
            data["graduationPercentageBps"] = _graduation_pct(circ_map.get(t), data.get("source"))
            results.append(data)

    return {"query": query, "sort": sort, "count": len(results), "results": results}
