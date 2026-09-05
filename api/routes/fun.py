from __future__ import annotations

import traceback
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from api.api import (
    _PCT_OF_SUPPLY,
    _api_source,
    _apply_live_pool_reserves,
    _build_ohlcv_from_db,
    _fmt,
    _fmt_usd,
    _lifecycle_fields,
    _nadfun_version,
    _quote_price_usd,
    _scaled_price,
    db_cursor,
    log,
    storage,
    time,
)
from api.routes.launchpad import _get_token_core_stats, _graduation_pct, _mon_usd_window

router = APIRouter()


def _trade_rows_out(rows: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for (
        log_index,
        ts_tr,
        user_address,
        is_buy,
        native_amount,
        token_amount,
        price_native,
        txhash,
        block_number,
    ) in rows:
        is_buy_flag = bool(is_buy)
        native_amount = int(native_amount or 0)
        token_amount = int(token_amount or 0)
        amount_in = native_amount if is_buy_flag else token_amount
        amount_out = token_amount if is_buy_flag else native_amount
        out.append(
            {
                "trade": {
                    "account": {"id": user_address},
                    "amountIn": str(amount_in),
                    "amountOut": str(amount_out),
                    "block": str(int(ts_tr)),
                    "timestamp": str(int(ts_tr)),
                    "blockNumber": str(int(block_number or 0)),
                    "logIndex": int(log_index),
                    "txhash": txhash,
                    "id": f"{txhash}-{log_index}",
                    "isBuy": is_buy_flag,
                    "priceNativePerTokenWad": _scaled_price(price_native),
                }
            }
        )
    return out


@router.get("/fun/token/{token_addr}/{chartres}")
def fun_token_overview(
    token_addr: str,
    chartres: int,
    tracked: str = Query(""),
    series: bool = Query(True),
) -> dict[str, Any]:
    from core.adapters import nadfun as _nadfun_geo

    t0 = time.time()
    try:
        if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 43200, 86400, 604800):
            raise HTTPException(status_code=400)

        token_addr = token_addr.lower()
        now_ts = int(time.time())
        day_ago = now_ts - 86400

        core = _get_token_core_stats(token_addr, day_ago)
        if core is None:
            raise HTTPException(status_code=404)

        creator = core["creator"]
        source = core["source"]
        created_at = core["created_at"]
        migrated_flag = bool(core["migrated"])
        market = core["market"]
        last_price_native = core["last_price_native"]
        quote_token = core["quote_token"]
        circulating_supply = core["circulating_supply"]
        snipers_count = core["snipers_count"]
        quote_price_usd = _quote_price_usd(quote_token)

        holders_count = int(core["holder_count"] or 0)
        volume_native_24h = int(core["volume_native_24h"] or 0)
        volume_usd_24h = core["volume_usd_24h"] or Decimal(0)
        buys_24h = int(core["buys_24h"] or 0)
        sells_24h = int(core["sells_24h"] or 0)

        dev_holding = 0
        if creator:
            with db_cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(balance_token), 0)
                    FROM launchpad_positions
                    WHERE token = %s AND user_address = %s
                    """,
                    (token_addr, creator),
                )
                row = cur.fetchone()
            dev_holding = int(row[0] or 0)

        marketcap_native_raw = last_price_native * Decimal(1e9)
        ath_price_native = core.get("ath_price_native") or Decimal(0)
        if ath_price_native < last_price_native:
            ath_price_native = last_price_native
        ath_marketcap = ath_price_native * Decimal(1e9)
        marketcap_usd = marketcap_native_raw * quote_price_usd if quote_price_usd > 0 else Decimal(0)

        mini_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=3600, max_buckets=24)

        series_klines = _build_ohlcv_from_db(token_addr, bucket_seconds=chartres, max_buckets=None) if series else []
        series_mon_usd = _mon_usd_window(int(series_klines[0]["time"]), now_ts) if series_klines else None

        with db_cursor() as cur:
            cur.execute(
                """
                SELECT log_index, timestamp, user_address, is_buy,
                       native_amount, token_amount, price_native, txhash, block_number
                FROM launchpad_trades
                WHERE token = %s
                ORDER BY timestamp DESC, block_number DESC, log_index DESC
                LIMIT 50
                """,
                (token_addr,),
            )
            trade_rows = cur.fetchall()
        trades_out = _trade_rows_out(trade_rows)

        tracked_addrs = sorted({a.strip().lower() for a in tracked.split(",") if a.strip()})
        tracked_trades_out: list[dict[str, Any]] = []
        if tracked_addrs:
            with db_cursor() as cur:
                cur.execute(
                    """
                    SELECT log_index, timestamp, user_address, is_buy,
                           native_amount, token_amount, price_native, txhash, block_number
                    FROM launchpad_trades
                    WHERE token = %s AND user_address = ANY(%s)
                    ORDER BY timestamp DESC, block_number DESC, log_index DESC
                    LIMIT 500
                    """,
                    (token_addr, tracked_addrs),
                )
                tracked_trades_out = _trade_rows_out(cur.fetchall())

        sniper_addresses: list[str] = []
        with db_cursor() as cur:
            cur.execute(
                "SELECT user_address FROM launchpad_snipers WHERE token = %s",
                (token_addr,),
            )
            sniper_addresses = [a for (a,) in cur.fetchall() if a]

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

        last_timestamp = int(trade_rows[0][1]) if trade_rows else (int(created_at or 0) or now_ts)

        _res = {
            "migrated": migrated_flag,
            "market": (market or "").lower(),
            "reserveQuote": str(int(core.get("curve_native_reserve") or 0)),
            "reserveBase": str(int(core.get("curve_token_reserve") or 0)),
        }
        _apply_live_pool_reserves({token_addr: _res})

        result = {
            "buyTxs": buys_24h,
            "creator": {
                "id": creator,
                "tokensGraduated": int(core["dev_tokens_graduated"]),
                "tokensLaunched": int(core["dev_tokens_created"]),
            },
            "decimals": 18,
            "description": core["description"] or "",
            "devHoldingAmount": str(dev_holding),
            "distinctBuyers": int(core["distinct_buyers"] or 0),
            "distinctSellers": int(core["distinct_sellers"] or 0),
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
            "metadataCID": core["metadata_cid"] or "",
            "imageUrl": core["metadata_cid"] or "",
            "migrated": migrated_flag,
            "migratedAt": core["migrated_at"],
            "mini": {"klines": mini_klines},
            "name": core["name"],
            "sellTxs": sells_24h,
            "monUsd": series_mon_usd,
            "series": {"klines": series_klines},
            "snipers": {
                "count": int(snipers_count or len(sniper_addresses)),
                "addresses": sorted(set(sniper_addresses)),
                "holdingShare": sniper_share,
            },
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
            "social1": core["social1"],
            "social2": core["social2"],
            "social3": core["social3"],
            "social4": core["social4"],
            "symbol": core["symbol"],
            "timestamp": str(int(created_at or 0)),
            "totalHolders": holders_count,
            "trackedtrades": tracked_trades_out,
            "trades": trades_out,
            "volumeNative": str(volume_native_24h),
            "volumeUsd": _fmt_usd(volume_usd_24h),
            "volume24hUsd": _fmt_usd(volume_usd_24h),
            "graduationPercentageBps": _graduation_pct(circulating_supply, source),
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
            "reserveQuote": _res["reserveQuote"],
            "reserveBase": _res["reserveBase"],
            "reservesFrom": _res.get("reservesFrom", "curve"),
            "reservesSyncedAt": _res.get("reservesSyncedAt", 0),
        }
        return result
    except HTTPException:
        raise
    except Exception:
        print(f"[fun_token_overview] error token={token_addr}")
        traceback.print_exc()
        raise
    finally:
        dt = (time.time() - t0) * 1000
        log.info("fun_token_overview token=%s chartres=%s dt_ms=%.1f", token_addr, chartres, dt)


def _fun_positions_by_wallet(addrs: list[str], include_token: str | None) -> dict[str, list[dict[str, Any]]]:
    where = "p.user_address = ANY(%s) AND (p.balance_token > 0"
    params: list[Any] = [addrs]
    if include_token:
        where += " OR p.token = %s"
        params.append(include_token)
    where += ")"

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                p.user_address, p.token, p.token_bought, p.token_sold,
                p.native_spent, p.native_received, p.balance_token,
                t.name, t.symbol, t.metadata_cid, t.last_price_native, t.market, t.source
            FROM launchpad_positions_live p
            JOIN launchpad_tokens t ON t.token = p.token
            WHERE {where}
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    out: dict[str, list[dict[str, Any]]] = {a: [] for a in addrs}
    for (
        user_address,
        token,
        token_bought,
        token_sold,
        native_spent,
        native_received,
        balance_token,
        name,
        symbol,
        metadata_cid,
        last_price_native,
        market,
        source,
    ) in rows:
        last_price_native = last_price_native or Decimal(0)
        balance_token = int(balance_token or 0)
        out.setdefault((user_address or "").lower(), []).append(
            {
                "token": token,
                "symbol": symbol,
                "name": name,
                "metadata_cid": metadata_cid or "",
                "balance_token": str(balance_token),
                "balance_native": _fmt(Decimal(balance_token) * last_price_native),
                "native_spent": str(int(native_spent or 0)),
                "native_received": str(int(native_received or 0)),
                "token_bought": str(int(token_bought or 0)),
                "token_sold": str(int(token_sold or 0)),
                "last_price_native": _fmt(last_price_native),
                "market": market or None,
                "source": _api_source(source),
                "nadfun_version": _nadfun_version(token, source),
            }
        )
    return out


@router.get("/fun/user/{user_addr}")
def fun_user_positions(user_addr: str, token: str = Query("")) -> dict[str, Any]:
    t0 = time.time()
    try:
        addr = user_addr.lower()
        tok = (token or "").strip().lower() or None
        by_wallet = _fun_positions_by_wallet([addr], tok)
        return {"user": addr, "positions": by_wallet.get(addr, [])}
    finally:
        log.info("fun_user_positions user=%s dt_ms=%.1f", user_addr, (time.time() - t0) * 1000)


@router.get("/fun/user")
def fun_users_positions_batch(
    addresses: str = Query(""),
    token: str = Query(""),
) -> dict[str, Any]:
    t0 = time.time()
    try:
        addrs: list[str] = []
        for a in (addresses or "").split(","):
            a = a.strip().lower()
            if a and a not in addrs:
                addrs.append(a)
        if not addrs:
            return {"users": {}, "count": 0}
        if len(addrs) > 100:
            raise HTTPException(status_code=400, detail="max 100 addresses")
        tok = (token or "").strip().lower() or None
        by_wallet = _fun_positions_by_wallet(addrs, tok)
        return {
            "users": {a: {"positions": by_wallet.get(a, [])} for a in addrs},
            "count": len(addrs),
        }
    finally:
        log.info(
            "fun_users_positions_batch n=%s dt_ms=%.1f",
            addresses.count(",") + 1 if addresses else 0,
            (time.time() - t0) * 1000,
        )
