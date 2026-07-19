# data fetchers for the websocket channels
#
# kept separate from api/ws.py so the transport owns connections and fanout while
# this module owns queries. every function here is synchronous and is called from
# the hub via asyncio.to_thread, because psycopg2 is blocking

from __future__ import annotations

from decimal import Decimal
from typing import Any

from api.api import _fmt, _fmt_usd, _internal_addrs, _quote_price_usd, _scaled_price
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


# shared row shape for the position derived channels
def _position_rows(token: str, where: str, params: tuple, limit: int) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT user_address, balance_token, token_bought, token_sold,
                   native_spent, native_received, realized_pnl_native,
                   unrealized_pnl_native, total_pnl_native, trade_count,
                   buy_count, sell_count
            FROM launchpad_positions
            WHERE {where}
            ORDER BY balance_token DESC
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


# everyone who has traded the token, ranked client side because ordering depends on
# the live price, which the client has and the backend does not
def top_traders(token: str) -> list[dict[str, Any]]:
    token = (token or "").lower()
    excluded = list(_internal_addrs())
    return _position_rows(
        token,
        "token = %s AND trade_count > 0 AND user_address <> ALL(%s)",
        (token, excluded),
        TOP_TRADERS_LIMIT,
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
    for r in rows:
        # balance in native terms, so the client does not need the price to render it
        r["balance_native"] = r["unrealized_pnl_native"]
        r["balance_usd"] = _fmt_usd(Decimal(r["unrealized_pnl_native"]) * quote) if quote > 0 else "0"
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

        cur.execute(
            """
            SELECT t.token, t.symbol, t.name, t.metadata_cid, t.last_price_native,
                   t.created_at, t.migrated, t.market,
                   (SELECT COUNT(*) FROM launchpad_positions p
                     WHERE p.token = t.token AND p.balance_token > 1) AS holders
            FROM launchpad_tokens t
            WHERE t.creator = %s
            ORDER BY t.created_at DESC NULLS LAST
            LIMIT %s
            """,
            (creator, DEV_TOKENS_LIMIT),
        )
        rows = cur.fetchall()

    out = []
    for tok, symbol, name, cid, price, created, migrated, market, holders in rows:
        out.append(
            {
                "id": tok,
                "symbol": symbol or "",
                "name": name or "",
                "metadataCID": cid or "",
                "imageUrl": cid or "",
                "lastPriceNativePerTokenWad": _scaled_price(price),
                "timestamp": str(int(created or 0)),
                "migrated": bool(migrated),
                "holders": int(holders or 0),
                "market": market,
            }
        )
    return out
