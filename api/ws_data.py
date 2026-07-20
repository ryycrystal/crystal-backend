# data fetchers for the websocket channels
#
# kept separate from api/ws.py so the transport owns connections and fanout while
# this module owns queries. every function here is synchronous and is called from
# the hub via asyncio.to_thread, because psycopg2 is blocking

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from api.api import (
    _WEI,
    _api_source,
    _fmt,
    _fmt_usd,
    _internal_addrs,
    _lifecycle_fields,
    _quote_price_usd,
    _scaled_price,
)
from core.storage import db_cursor

# how many rows each list channel carries
TRADES_LIMIT = 50
HOLDERS_LIMIT = 50
TOP_TRADERS_LIMIT = 50
DEV_TOKENS_LIMIT = 50


# highest block the indexer has committed, the watermark every frame is stamped with
def indexer_watermark() -> int:
    with db_cursor() as cur:
        cur.execute("SELECT MAX(number) FROM launchpad_blocks;")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# most recent trades for a token, newest first
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
                # decimal log index, matching the REST id format exactly. the client
                # normalises hex ids from its chain socket against this, so a third
                # format here would silently double count
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


# last traded price for a token, used to value positions
def _last_price(token: str) -> Decimal:
    with db_cursor() as cur:
        cur.execute("SELECT last_price_native FROM launchpad_tokens WHERE token = %s", (token,))
        row = cur.fetchone()
    return (row[0] if row and row[0] is not None else Decimal(0)) or Decimal(0)


# shared row shape for the position derived channels
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


# top holders by balance, excluding internal addresses
def top_holders(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    excluded = list(_internal_addrs())
    return _position_rows(
        token,
        "token = %s AND balance_token > 0 AND user_address <> ALL(%s)",
        (token, excluded),
        HOLDERS_LIMIT,
    )


# everyone who has traded the token, ranked by pnl to match the rest endpoint.
# ordering by balance truncated the list to the biggest current holders, so a trader
# who sold out at a large profit ranked last and fell off the list entirely
def top_traders(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    excluded = list(_internal_addrs())
    return _position_rows(
        token,
        "token = %s AND trade_count > 0 AND user_address <> ALL(%s)",
        (token, excluded),
        TOP_TRADERS_LIMIT,
        order_by="total_pnl_native DESC",
    )


# positions for a specific wallet set, since positions are per wallet not per token
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
        # balance in native terms, so the client does not need the price to render it.
        # this used to report unrealized pnl, so a position showed its profit where
        # its value belonged, and went negative for anyone underwater
        value_native = Decimal(r["balance_token"] or 0) * price
        r["balance_native"] = _fmt(value_native)
        r["balance_usd"] = _fmt_usd(value_native * quote / _WEI) if quote > 0 else "0"
    return rows


# tokens launched by this token's creator
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
                # marketcap and source were on the rest payload but not here, so both
                # blanked out on the first update that replaced the array
                "marketcap": _fmt(marketcap),
                "source": _api_source(source),
                "volumeNative1h": str(int(vol_1h or 0)),
                "timestamp": str(int(created or 0)),
                "migrated": bool(migrated),
                "holders": int(holders or 0),
                "market": market,
            }
        )
    return out


# everything on the token detail response that moves as trades land, so a client can
# stop polling /token entirely after its first load. the static half of that response
# (name, symbol, socials, creator, image) never changes and is deliberately omitted
def token_state(token: str) -> dict[str, Any]:
    token = (token or "").lower()
    excluded = list(_internal_addrs())
    day_ago = int(time.time()) - 86400
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                t.last_price_native, t.native_volume, t.token_volume, t.volume_usd,
                t.fees_usd, t.buy_count, t.sell_count, t.tx_count,
                t.circulating_supply, t.source, t.migrated, t.market, t.migrated_at,
                t.approaching_75, t.approaching_75_block, t.approaching_75_at,
                COALESCE(t.ath_price_native, 0),
                t.curve_native_reserve, t.curve_token_reserve,
                (SELECT COUNT(*) FROM launchpad_positions p
                  WHERE p.token = t.token AND p.balance_token > 1
                    AND p.user_address <> ALL(%s)),
                (SELECT COUNT(*) FROM launchpad_positions p
                  WHERE p.token = t.token AND p.buy_count > 0
                    AND p.user_address <> ALL(%s)),
                (SELECT COUNT(*) FROM launchpad_positions p
                  WHERE p.token = t.token AND p.sell_count > 0
                    AND p.user_address <> ALL(%s)),
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
        # price and valuation
        "lastPriceNativePerTokenWad": _scaled_price(last_price),
        "lastPriceQuotePerTokenWad": _scaled_price(last_price),
        "marketcap": _fmt(marketcap),
        "marketcap_quote": _fmt(marketcap),
        "marketcap_usd": _fmt_usd(marketcap * quote_usd) if quote_usd > 0 else "0",
        # all time high, only moves on a new high
        "athPriceNative": _fmt(ath_price),
        "athMarketcap": _fmt(ath_marketcap),
        "athMarketcapUsd": _fmt_usd(ath_marketcap * quote_usd) if quote_usd > 0 else "0",
        # 24h, matching what the rest endpoint returns under these same names. the
        # names are historical and do say "volume" rather than "volume24h"
        "volumeNative": str(int(volume_native_24h or 0)),
        "volume_usd": _fmt_usd(volume_usd_24h or Decimal(0)),
        "buyTxs": int(buys_24h or 0),
        "sellTxs": int(sells_24h or 0),
        # lifetime, under names that say so
        "volumeNativeLifetime": str(int(native_volume or 0)),
        "tokenVolume": str(int(token_volume or 0)),
        "volumeUsdLifetime": _fmt_usd(volume_usd or Decimal(0)),
        "buyTxsLifetime": int(buys or 0),
        "sellTxsLifetime": int(sells or 0),
        "fees_usd": _fmt_usd(fees_usd or Decimal(0)),
        "txCount": int(tx_count or 0),
        # participants
        "totalHolders": int(holders or 0),
        "distinctBuyers": int(distinct_buyers or 0),
        "distinctSellers": int(distinct_sellers or 0),
        # curve position, the progress bar
        "circulating_supply": str(int(circulating or 0)),
        "curveNativeReserve": str(int(curve_native or 0)),
        "curveTokenReserve": str(int(curve_token or 0)),
        # the 75% threshold flag and when it fired
        "approaching_75": bool(approaching),
        # null rather than 0 when the threshold was never crossed, matching rest. a
        # zero here renders as january 1970 wherever the client formats it as a date
        "approaching_75_block": int(approaching_block) if approaching_block else None,
        "approaching_75_at": int(approaching_at) if approaching_at else None,
        # graduation. these flip exactly once but the page must react immediately
        "migrated": bool(migrated),
        "market": market,
        "migratedAt": int(migrated_at) if migrated_at else None,
    }

    # phase and progress are derived, never stored, so they cannot drift
    body.update(
        _lifecycle_fields(
            source=source,
            circulating_supply=int(circulating or 0),
            tx_count=int(tx_count or 0),
            migrated=bool(migrated),
        )
    )
    return body
