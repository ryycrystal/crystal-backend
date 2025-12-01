from __future__ import annotations
from decimal import Decimal, getcontext
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

AGGREGATOR_ADDR = "0x0B79d71AE99528D1dB24A4148b5f4F865cc2b137".lower()

def _internal_addrs() -> set[str]:
    base: set[str] = {AGGREGATOR_ADDR}
    base.update(a.lower() for a in getattr(h, "ADDRS", []))

    for pool, _, _, _ in storage.load_all_pools():
        if pool:
            base.add(pool.lower())

    return base

def _holders_for_token(token_addr: str, creator: str | None) -> Tuple[int, int, int]:
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
    filtered: List[int] = []

    for ua, bal in rows:
        ua = ua.lower()
        bal = int(bal or 0)

        if ua == creator_addr:
            dev_holding = bal

        if ua not in excluded:
            filtered.append(bal)

    filtered.sort(reverse=True)
    holder_count = len(filtered)
    top10_holding = sum(filtered[:10])

    return holder_count, dev_holding, top10_holding


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
    holders, dev_holding, top10_holding = _holders_for_token(token, creator)

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
        "native_volume": str(native_volume),
        "token_volume": str(token_volume),
        "volume_usd": str(volume_usd),
        "fees_usd": str(fees_usd),
        "marketcap_native_raw": str(marketcap_native_raw),
        "marketcap_usd": str(marketcap_usd),
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

    recent_created_out: List[Dict[str, Any]] = []
    recent_approaching_out: List[Dict[str, Any]] = []
    recent_graduated_out: List[Dict[str, Any]] = []

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, circulating_supply
            FROM launchpad_tokens
            WHERE migrated = TRUE
            ORDER BY migrated_at DESC NULLS LAST, migrated_block DESC NULLS LAST
            LIMIT 30
            """
        )
        grad_rows = cur.fetchall()

    graduated_ids = {t.lower() for (t, _) in grad_rows}

    for token, circ in grad_rows:
        row = _serialize_token(token)
        row["graduationPercentageBps"] = (circ or 0) / 793100000
        recent_graduated_out.append(row)

    with db_cursor() as cur:
        if graduated_ids:
            cur.execute(
                """
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                AND migrated = FALSE
                AND token <> ALL(%s)
                ORDER BY (circulating_supply::numeric / 793100000) DESC
                LIMIT 30
                """,
                (list(graduated_ids),),
            )
        else:
            cur.execute(
                """
                SELECT token, circulating_supply
                FROM launchpad_tokens
                WHERE approaching_75 = TRUE
                AND migrated = FALSE
                ORDER BY (circulating_supply::numeric / 793100000) DESC
                LIMIT 30
                """
            )
        appr_rows = cur.fetchall()

    approaching_ids = {t.lower() for (t, _) in appr_rows}

    for token, circ in appr_rows:
        row = _serialize_token(token)
        row["graduationPercentageBps"] = (circ or 0) / 793100000
        recent_approaching_out.append(row)

    excluded_ids = graduated_ids | approaching_ids

    with db_cursor() as cur:
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
            cur.execute(
                """
                SELECT token, circulating_supply
                FROM launchpad_tokens
                ORDER BY created_at DESC NULLS LAST, created_block DESC NULLS LAST
                LIMIT 30
                """
            )
        created_rows = cur.fetchall()

    for token, circ in created_rows:
        row = _serialize_token(token)
        row["graduationPercentageBps"] = (circ or 0) / 793100000
        recent_created_out.append(row)

    result = {
        "recent_created": recent_created_out,
        "recent_approaching": recent_approaching_out,
        "recent_graduated": recent_graduated_out,
    }

    dt = (time.time() - t0) * 1000
    log.info("token_list dt_ms=%.1f", dt)

    return result


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

        if row is None:
            raise HTTPException(status_code=404)

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

        creator = (creator or "").lower()
        last_price_native = last_price_native or Decimal(0)
        mon_price = _mon_price_usd()

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE buy_count > 0),
                    COUNT(*) FILTER (WHERE sell_count > 0)
                FROM launchpad_positions
                WHERE token = %s AND user_address <> ALL(%s)
                """,
                (token_addr, list(excluded)),
            )
            buyers_sellers = cur.fetchone()

        distinct_buyers = int(buyers_sellers[0] or 0)
        distinct_sellers = int(buyers_sellers[1] or 0)

        holders_count, dev_holding, _top10 = _holders_for_token(token_addr, creator)

        decimals = 18

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
                    (creator,),
                )
                row_user = cur.fetchone()
            if row_user is not None:
                dev_tokens_created = row_user[0] or 0
                dev_tokens_graduated = row_user[1] or 0

        last_price_wad = last_price_native * Decimal(1e9)
        marketcap_native_raw = last_price_native * Decimal(1e9)
        marketcap_usd = marketcap_native_raw * mon_price if mon_price > 0 else Decimal(0)

        volume_native = native_volume or 0
        volume_token = token_volume or 0
        volume_usd_val = volume_usd or Decimal(0)

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
                    "balance_native": str(current_value_native),
                    "balance_usd": str(balance_usd),
                    "native_spent": str(native_spent),
                    "native_received": str(native_received),
                    "realized_pnl_native": str(realized_pnl),
                    "unrealized_pnl_native": str(unrealized_pnl),
                    "total_pnl_native": str(total_pnl),
                    "total_pnl_usd": str(total_pnl_usd),
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
                    "balance_native": str(current_value_native),
                    "balance_usd": str(balance_usd),
                    "native_spent": str(native_spent),
                    "native_received": str(native_received),
                    "realized_pnl_native": str(realized_pnl),
                    "unrealized_pnl_native": str(unrealized_pnl),
                    "total_pnl_native": str(total_pnl),
                    "total_pnl_usd": str(total_pnl_usd),
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

        volume_native_str = str(int(volume_native or 0))
        volume_token_str = str(int(volume_token or 0))
        volume_usd_str = str(volume_usd_val)

        dev_tokens_list: List[Dict[str, Any]] = []
        dev_tokens_total = 0
        if creator:
            now_ts = int(time.time())
            cutoff_ts = now_ts - 3600

            with db_cursor() as cur:
                # Get total count of dev tokens
                cur.execute(
                    "SELECT COUNT(*) FROM launchpad_tokens WHERE creator = %s",
                    (creator,),
                )
                dev_tokens_total = cur.fetchone()[0] or 0

                # Get 50 newest dev tokens
                cur.execute(
                    """
                    SELECT
                        token,
                        name,
                        symbol,
                        metadata_cid,
                        last_price_native,
                        migrated,
                        created_at
                    FROM launchpad_tokens
                    WHERE creator = %s
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT 50
                    """,
                    (creator,),
                )
                dev_token_rows = cur.fetchall()

            for (
                dev_token_addr,
                dev_name,
                dev_symbol,
                dev_metadata_cid,
                dev_last_price_native,
                dev_migrated,
                dev_created_at,
            ) in dev_token_rows:
                dev_last_price_native = dev_last_price_native or Decimal(0)
                dev_price_wad = dev_last_price_native * Decimal(1e9)
                dev_marketcap_native = dev_last_price_native * Decimal(1e9)

                with db_cursor() as cur:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(native_amount), 0)
                        FROM launchpad_trades
                        WHERE token = %s AND timestamp >= %s
                        """,
                        (dev_token_addr.lower(), cutoff_ts),
                    )
                    vol_row = cur.fetchone()
                vol_1h_native = int(vol_row[0] or 0)

                dev_total_holders, _, _ = _holders_for_token(dev_token_addr.lower(), creator)

                dev_tokens_list.append(
                    {
                        "id": dev_token_addr,
                        "name": dev_name,
                        "symbol": dev_symbol,
                        "metadataCID": dev_metadata_cid or "",
                        "lastPriceNativePerTokenWad": str(dev_price_wad),
                        "marketcap": str(dev_marketcap_native),
                        "migrated": bool(dev_migrated),
                        "volumeNative1h": str(vol_1h_native),
                        "holders": int(dev_total_holders),
                        "timestamp": str(int(dev_created_at or 0)),
                    }
                )

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
            "buyTxs": int(buy_count or 0),
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
            "sellTxs": int(sell_count or 0),
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
            "trackedtrades": tracked_trades_out,
            "trades": trades_out,
            "volumeNative": volume_native_str,
            "volumeToken": volume_token_str,
            "volumeUsd": volume_usd_str,
            "graduationPercentageBps": graduation_bps,
            "circulating_supply": str(int(circulating_supply or 0)),
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
                t.last_price_native
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
                "balance_native": str(current_value_native),
                "balance_usd": str(current_value_usd),
                "native_spent": str(native_spent),
                "native_received": str(native_received),
                "realized_pnl_native": str(realized_pnl),
                "unrealized_pnl_native": str(unrealized_pnl_val),
                "total_pnl_native": str(total_pnl),
                "total_pnl_usd": str(total_pnl_usd),
                "trade_count": int(trade_count or 0),
                "buy_count": int(buy_count or 0),
                "sell_count": int(sell_count or 0),
                "token_bought": str(token_bought),
                "token_sold": str(token_sold),
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
        "portfolio_value_native": str(total_value_native),
        "portfolio_value_usd": str(total_value_usd),
        "realized_pnl_native": str(total_realized_pnl),
        "unrealized_pnl_native": str(total_unrealized_pnl),
        "total_pnl_native": str(total_pnl_native_val),
        "total_pnl_usd": str(total_pnl_usd),
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

    if sort is None:
        rows = storage.search_tokens(q, limit=50)
        results: List[Dict[str, Any]] = []

        for token, circ_supply, _score in rows:
            token_addr = (token or "").lower()
            if not token_addr:
                continue

            row = _serialize_token(token_addr)
            if not row:
                continue

            graduation_bps = (circ_supply or 0) / 793100000
            row["graduationPercentageBps"] = graduation_bps
            results.append(row)

        return {
            "query": query,
            "sort": None,
            "count": len(results),
            "results": results,
        }

    rows = storage.search_tokens(q, limit=1000)
    token_addrs = [(token or "").lower() for token, _, _ in rows if token]

    if not token_addrs:
        return {
            "query": query,
            "sort": sort,
            "count": 0,
            "results": [],
        }

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
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(native_amount), 0) as vol
                    FROM launchpad_trades
                    WHERE token = t.token AND timestamp >= %s
                ) tr ON true
                WHERE t.token = ANY(%s)
                ORDER BY tr.vol DESC NULLS LAST
                LIMIT 50
                """,
                (cutoff_1h, token_addrs),
            )
        elif sort == "volume_24h":
            cutoff_24h = now_ts - 86400
            cur.execute(
                """
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
                """,
                (cutoff_24h, token_addrs),
            )
        elif sort == "holders":
            cur.execute(
                """
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
                """,
                (token_addrs,),
            )
        else:
            raise HTTPException(status_code=400, detail=f"invalid sort: {sort}. Use 'mc', 'volume_1h', 'volume_24h', 'recent', or 'holders'")

        sorted_rows = cur.fetchall()

    results = []
    for token, _price, circ_supply in sorted_rows:
        token_addr = (token or "").lower()
        if not token_addr:
            continue

        row = _serialize_token(token_addr)
        if not row:
            continue

        graduation_bps = (circ_supply or 0) / 793100000
        row["graduationPercentageBps"] = graduation_bps
        results.append(row)

    return {
        "query": query,
        "sort": sort,
        "count": len(results),
        "results": results,
    }


def _mon_price_usd() -> Decimal:
    try:
        px = storage.get_mon_price_usd()
        if px is None:
            return Decimal("0.03")
        px_dec = Decimal(px)
        if px_dec <= 0:
            return Decimal("0.03")
        return px_dec
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
