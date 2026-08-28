from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from api.api import (
    _WEI,
    _api_source,
    _fmt,
    _fmt_usd,
    _lifecycle_fields,
    _nadfun_version,
    _quote_price_usd,
    _scaled_price,
    _sql_not_internal,
    _static_internal_addrs,
)
from core.storage import db_cursor

TRADES_LIMIT = 50
HOLDERS_LIMIT = 50
TOP_TRADERS_LIMIT = 50
DEV_TOKENS_LIMIT = 50


def indexer_watermark() -> int:
    with db_cursor() as cur:
        cur.execute("SELECT MAX(number) FROM launchpad_blocks;")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def recent_trades(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT txhash, log_index, timestamp, block_number, user_address,
                   is_buy, native_amount, token_amount, usd_amount, price_native
            FROM launchpad_trades
            WHERE token = %s
            ORDER BY timestamp DESC, log_index DESC
            LIMIT %s
            """,
            (token, TRADES_LIMIT),
        )
        rows = cur.fetchall()

    out = []
    for txhash, log_index, ts, blk, caller, is_buy, native_amt, token_amt, usd_amt, price in rows:
        out.append(
            {
                "id": f"{txhash}-{int(log_index)}",
                "time": int(ts or 0),
                "blockNumber": int(blk or 0),
                "caller": (caller or "").lower(),
                "isBuy": bool(is_buy),
                "nativeAmount": str(int(native_amt or 0)),
                "tokenAmount": str(int(token_amt or 0)),
                "usdAmount": _fmt_usd(usd_amt or Decimal(0)),
                "price": _scaled_price(price),
            }
        )
    return out


def _last_price(token: str) -> Decimal:
    with db_cursor() as cur:
        cur.execute("SELECT last_price_native FROM launchpad_tokens WHERE token = %s", (token,))
        row = cur.fetchone()
    return (row[0] if row and row[0] is not None else Decimal(0)) or Decimal(0)


def _position_rows(
    token: str, where: str, params: tuple, limit: int, order_by: str = "balance_token DESC"
) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT user_address, balance_token, token_bought, token_sold,
                   native_spent, native_received, realized_pnl_native,
                   unrealized_pnl_native, total_pnl_native, trade_count,
                   buy_count, sell_count
            FROM launchpad_positions
            WHERE {where}
            ORDER BY {order_by}
            LIMIT %s
            """,
            params + (limit,),
        )
        rows = cur.fetchall()

    out = []
    for (
        addr,
        balance,
        bought,
        sold,
        spent,
        received,
        realized,
        unrealized,
        total,
        trades,
        buys,
        sells,
    ) in rows:
        out.append(
            {
                "address": (addr or "").lower(),
                "balance_token": str(int(balance or 0)),
                "token_bought": str(int(bought or 0)),
                "token_sold": str(int(sold or 0)),
                "native_spent": str(int(spent or 0)),
                "native_received": str(int(received or 0)),
                "realized_pnl_native": _fmt(realized or Decimal(0)),
                "unrealized_pnl_native": _fmt(unrealized or Decimal(0)),
                "total_pnl_native": _fmt(total or Decimal(0)),
                "trade_count": int(trades or 0),
                "buy_count": int(buys or 0),
                "sell_count": int(sells or 0),
            }
        )
    return out


def top_holders(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    excluded = list(_static_internal_addrs())
    return _position_rows(
        token,
        f"token = %s AND balance_token > 0 AND user_address <> ALL(%s) AND {_sql_not_internal('user_address')}",
        (token, excluded),
        HOLDERS_LIMIT,
    )


def top_traders(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    excluded = list(_static_internal_addrs())
    return _position_rows(
        token,
        f"token = %s AND trade_count > 0 AND user_address <> ALL(%s) AND {_sql_not_internal('user_address')}",
        (token, excluded),
        TOP_TRADERS_LIMIT,
        order_by="total_pnl_native DESC",
    )


def positions_for(token: str, addresses: list[str]) -> list[dict[str, Any]]:
    token = (token or "").lower()
    addrs = [a.lower() for a in addresses if a]
    if not addrs:
        return []
    rows = _position_rows(
        token,
        "token = %s AND user_address = ANY(%s)",
        (token, addrs),
        max(len(addrs), 1),
    )
    quote = _quote_price_usd(None)
    price = _last_price(token)
    for r in rows:
        value_native = Decimal(r["balance_token"] or 0) * price
        r["balance_native"] = _fmt(value_native)
        r["balance_usd"] = _fmt_usd(value_native * quote / _WEI) if quote > 0 else "0"
    return rows


def positions_for_wallets(addresses: list[str]) -> list[dict[str, Any]]:
    addrs = [a.lower() for a in addresses if a]
    if not addrs:
        return []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.user_address, p.token, p.balance_token, p.token_bought, p.token_sold,
                   p.native_spent, p.native_received, p.realized_pnl_native,
                   p.unrealized_pnl_native, p.total_pnl_native, p.trade_count,
                   p.buy_count, p.sell_count,
                   t.name, t.symbol, t.metadata_cid, t.last_price_native, t.market, t.source
            FROM launchpad_positions p
            JOIN launchpad_tokens t ON t.token = p.token
            WHERE p.user_address = ANY(%s)
            """,
            (addrs,),
        )
        rows = cur.fetchall()

    quote = _quote_price_usd(None)
    out = []
    for (
        addr,
        token,
        balance,
        bought,
        sold,
        spent,
        received,
        realized,
        unrealized,
        total,
        trades,
        buys,
        sells,
        name,
        symbol,
        cid,
        last_price,
        market,
        source,
    ) in rows:
        last_price = last_price or Decimal(0)
        value_native = Decimal(int(balance or 0)) * last_price
        out.append(
            {
                "address": (addr or "").lower(),
                "token": (token or "").lower(),
                "symbol": symbol,
                "name": name,
                "metadata_cid": cid or "",
                "balance_token": str(int(balance or 0)),
                "balance_native": _fmt(value_native),
                "balance_usd": _fmt_usd(value_native * quote / _WEI) if quote > 0 else "0",
                "last_price_native": _fmt(last_price),
                "token_bought": str(int(bought or 0)),
                "token_sold": str(int(sold or 0)),
                "native_spent": str(int(spent or 0)),
                "native_received": str(int(received or 0)),
                "realized_pnl_native": _fmt(realized or Decimal(0)),
                "unrealized_pnl_native": _fmt(unrealized or Decimal(0)),
                "total_pnl_native": _fmt(total or Decimal(0)),
                "trade_count": int(trades or 0),
                "buy_count": int(buys or 0),
                "sell_count": int(sells or 0),
                "market": market or None,
                "source": _api_source(source),
                "nadfunVersion": _nadfun_version(token, source),
            }
        )
    return out


def dev_tokens(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    with db_cursor() as cur:
        cur.execute("SELECT creator FROM launchpad_tokens WHERE token = %s", (token,))
        row = cur.fetchone()
        creator = (row[0] or "").lower() if row else ""
        if not creator:
            return []

        hour_ago = int(time.time()) - 3600
        cur.execute(
            """
            SELECT t.token, t.symbol, t.name, t.metadata_cid, t.last_price_native,
                   t.created_at, t.migrated, t.market, t.source,
                   (SELECT COUNT(*) FROM launchpad_positions p
                     WHERE p.token = t.token AND p.balance_token > 1) AS holders,
                   -- the strip renders a 1h volume column, and it was the one number
                   -- the channel could not supply, so it fell back to a stale rest read
                   (SELECT COALESCE(SUM(native_amount), 0) FROM launchpad_trades
                     WHERE token = t.token AND timestamp >= %s) AS vol_1h
            FROM launchpad_tokens t
            WHERE t.creator = %s
            ORDER BY t.created_at DESC NULLS LAST
            LIMIT %s
            """,
            (hour_ago, creator, DEV_TOKENS_LIMIT),
        )
        rows = cur.fetchall()

    out = []
    for tok, symbol, name, cid, price, created, migrated, market, source, holders, vol_1h in rows:
        marketcap = (price or Decimal(0)) * Decimal(10**9)
        out.append(
            {
                "id": tok,
                "symbol": symbol or "",
                "name": name or "",
                "metadataCID": cid or "",
                "imageUrl": cid or "",
                "lastPriceNativePerTokenWad": _scaled_price(price),
                "marketcap": _fmt(marketcap),
                "source": _api_source(source),
                "nadfunVersion": _nadfun_version(tok, source),
                "volumeNative1h": str(int(vol_1h or 0)),
                "timestamp": str(int(created or 0)),
                "migrated": bool(migrated),
                "holders": int(holders or 0),
                "market": market,
            }
        )
    return out


def token_state(token: str) -> dict[str, Any]:
    token = (token or "").lower()
    excluded = list(_static_internal_addrs())
    not_internal = _sql_not_internal("p.user_address")
    day_ago = int(time.time()) - 86400
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                t.last_price_native, t.native_volume, t.token_volume, t.volume_usd,
                t.fees_usd, t.buy_count, t.sell_count, t.tx_count,
                t.circulating_supply, t.source, t.migrated, t.market, t.migrated_at,
                t.approaching_75, t.approaching_75_block, t.approaching_75_at,
                COALESCE(t.ath_price_native, 0),
                t.curve_native_reserve, t.curve_token_reserve,
                -- a graduated token's liquidity sits in whichever venue it
                -- migrated to: nadfun tokens land in an amm pair, crystal
                -- launchpad tokens land in a crystal market. both are reserves
                -- and the client should not have to know which one applies
                COALESCE(
                    (SELECT lp.reserve_native FROM launchpad_pools lp WHERE lp.token_addr = t.token
                      ORDER BY lp.last_sync_at DESC NULLS LAST LIMIT 1),
                    (SELECT cp.reserve_quote FROM crystal_pools cp WHERE cp.market = t.market)
                ),
                COALESCE(
                    (SELECT lp.reserve_token FROM launchpad_pools lp WHERE lp.token_addr = t.token
                      ORDER BY lp.last_sync_at DESC NULLS LAST LIMIT 1),
                    (SELECT cp.reserve_base FROM crystal_pools cp WHERE cp.market = t.market)
                ),
                (SELECT COUNT(*) FROM launchpad_positions p
                  WHERE p.token = t.token AND p.balance_token > 1
                    AND p.user_address <> ALL(%s) AND {not_internal}),
                (SELECT COUNT(*) FROM launchpad_positions p
                  WHERE p.token = t.token AND p.buy_count > 0
                    AND p.user_address <> ALL(%s) AND {not_internal}),
                (SELECT COUNT(*) FROM launchpad_positions p
                  WHERE p.token = t.token AND p.sell_count > 0
                    AND p.user_address <> ALL(%s) AND {not_internal}),
                -- the rest endpoint reports these over 24h under the same key names.
                -- pushing lifetime totals here made the page jump by orders of
                -- magnitude a moment after it loaded
                (SELECT COALESCE(SUM(native_amount), 0) FROM launchpad_trades
                  WHERE token = t.token AND timestamp >= %s),
                (SELECT COALESCE(SUM(usd_amount), 0) FROM launchpad_trades
                  WHERE token = t.token AND timestamp >= %s),
                (SELECT COUNT(*) FROM launchpad_trades
                  WHERE token = t.token AND timestamp >= %s AND is_buy),
                (SELECT COUNT(*) FROM launchpad_trades
                  WHERE token = t.token AND timestamp >= %s AND NOT is_buy)
            FROM launchpad_tokens t
            WHERE t.token = %s
            """,
            (excluded, excluded, excluded, day_ago, day_ago, day_ago, day_ago, token),
        )
        row = cur.fetchone()

    if not row:
        return {}

    (
        last_price,
        native_volume,
        token_volume,
        volume_usd,
        fees_usd,
        buys,
        sells,
        tx_count,
        circulating,
        source,
        migrated,
        market,
        migrated_at,
        approaching,
        approaching_block,
        approaching_at,
        ath_price,
        curve_native,
        curve_token,
        pool_native,
        pool_token,
        holders,
        distinct_buyers,
        distinct_sellers,
        volume_native_24h,
        volume_usd_24h,
        buys_24h,
        sells_24h,
    ) = row

    last_price = last_price or Decimal(0)
    ath_price = ath_price or Decimal(0)
    if ath_price < last_price:
        ath_price = last_price

    quote_usd = _quote_price_usd(None)
    marketcap = last_price * Decimal(10**9)
    ath_marketcap = ath_price * Decimal(10**9)

    body: dict[str, Any] = {
        "lastPriceNativePerTokenWad": _scaled_price(last_price),
        "lastPriceQuotePerTokenWad": _scaled_price(last_price),
        "marketcap": _fmt(marketcap),
        "marketcap_quote": _fmt(marketcap),
        "marketcap_usd": _fmt_usd(marketcap * quote_usd) if quote_usd > 0 else "0",
        "athPriceNative": _fmt(ath_price),
        "athMarketcap": _fmt(ath_marketcap),
        "athMarketcapUsd": _fmt_usd(ath_marketcap * quote_usd) if quote_usd > 0 else "0",
        "volumeNative": str(int(volume_native_24h or 0)),
        "volume_usd": _fmt_usd(volume_usd_24h or Decimal(0)),
        "buyTxs": int(buys_24h or 0),
        "sellTxs": int(sells_24h or 0),
        "volumeNativeLifetime": str(int(native_volume or 0)),
        "tokenVolume": str(int(token_volume or 0)),
        "volumeUsdLifetime": _fmt_usd(volume_usd or Decimal(0)),
        "buyTxsLifetime": int(buys or 0),
        "sellTxsLifetime": int(sells or 0),
        "fees_usd": _fmt_usd(fees_usd or Decimal(0)),
        "txCount": int(tx_count or 0),
        "totalHolders": int(holders or 0),
        "distinctBuyers": int(distinct_buyers or 0),
        "distinctSellers": int(distinct_sellers or 0),
        "circulating_supply": str(int(circulating or 0)),
        "curveNativeReserve": str(int(curve_native or 0)),
        "curveTokenReserve": str(int(curve_token or 0)),
        "poolNativeReserve": str(int(pool_native or 0)),
        "poolTokenReserve": str(int(pool_token or 0)),
        "approaching_75": bool(approaching),
        "approaching_75_block": int(approaching_block) if approaching_block else None,
        "approaching_75_at": int(approaching_at) if approaching_at else None,
        "migrated": bool(migrated),
        "market": market,
        "migratedAt": int(migrated_at) if migrated_at else None,
    }

    body.update(
        _lifecycle_fields(
            source=source,
            circulating_supply=int(circulating or 0),
            tx_count=int(tx_count or 0),
            migrated=bool(migrated),
        )
    )
    return body
