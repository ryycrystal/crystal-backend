from __future__ import annotations

import re
import time
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json, execute_values

from .base import db_cursor

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"


# mark one block as indexed
def record_block_processed(block_number: int, cur: psycopg2.extensions.cursor | None = None) -> None:
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_blocks (number)
                VALUES (%s)
                ON CONFLICT (number) DO UPDATE
                SET processed_at = NOW();
                """,
                (block_number,),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_blocks (number)
            VALUES (%s)
            ON CONFLICT (number) DO UPDATE
            SET processed_at = NOW();
            """,
            (block_number,),
        )


# mark many blocks as indexed in one statement
def record_blocks_processed_batch(block_numbers: list[int], cur: psycopg2.extensions.cursor | None = None) -> None:
    if not block_numbers:
        return
    rows = [(int(b),) for b in block_numbers]
    query = """
        INSERT INTO launchpad_blocks (number)
        VALUES %s
        ON CONFLICT (number) DO UPDATE
        SET processed_at = NOW();
    """
    if cur is None:
        with db_cursor() as cur2:
            execute_values(cur2, query, rows, page_size=10000)
    else:
        execute_values(cur, query, rows, page_size=10000)


# highest block the indexer has committed
def get_last_processed_block() -> str | None:
    with db_cursor() as cur:
        cur.execute("SELECT MAX(number) FROM launchpad_blocks;")
        row = cur.fetchone()

    if row is None:
        return None

    last = row[0]
    return int(last) if last is not None else None


# write one trade row, ignoring a duplicate txhash and log index
def insert_trade(
    *,
    block_number: int,
    log_index: int,
    timestamp: int,
    token: str,
    user_address: str,
    is_buy: bool,
    native_amount: int,
    token_amount: int,
    usd_amount,
    price_native,
    txhash: str,
    native_reserve=0,
    token_reserve=0,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_trades (
                    block_number,
                    log_index,
                    timestamp,
                    token,
                    user_address,
                    is_buy,
                    native_amount,
                    token_amount,
                    usd_amount,
                    price_native,
                    txhash,
                    native_reserve,
                    token_reserve
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (txhash, log_index) DO NOTHING;
                """,
                (
                    int(block_number),
                    int(log_index),
                    int(timestamp),
                    token,
                    user_address,
                    bool(is_buy),
                    int(native_amount),
                    int(token_amount),
                    usd_amount,
                    price_native,
                    txhash,
                    int(native_reserve or 0),
                    int(token_reserve or 0),
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_trades (
                block_number,
                log_index,
                timestamp,
                token,
                user_address,
                is_buy,
                native_amount,
                token_amount,
                usd_amount,
                price_native,
                txhash,
                native_reserve,
                token_reserve
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (txhash, log_index) DO NOTHING;
            """,
            (
                int(block_number),
                int(log_index),
                int(timestamp),
                token,
                user_address,
                bool(is_buy),
                int(native_amount),
                int(token_amount),
                usd_amount,
                price_native,
                txhash,
                int(native_reserve or 0),
                int(token_reserve or 0),
            ),
        )


def aggregate_token_from_trades(token: str, cur: psycopg2.extensions.cursor | None = None) -> dict:
    tok = (token or "").lower()

    # run the aggregate query on a cursor
    def _run(c):
        c.execute(
            """
            SELECT
                COALESCE(SUM(native_amount), 0),
                COALESCE(SUM(token_amount), 0),
                COALESCE(SUM(usd_amount), 0),
                COUNT(*),
                COALESCE(SUM(CASE WHEN is_buy THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN is_buy THEN 0 ELSE 1 END), 0)
            FROM launchpad_trades WHERE token = %s;
            """,
            (tok,),
        )
        native_volume, token_volume, volume_usd, tx_count, buys, sells = c.fetchone()
        c.execute(
            """
            SELECT price_native, native_reserve, token_reserve
            FROM launchpad_trades WHERE token = %s
            ORDER BY block_number DESC, log_index DESC
            LIMIT 1;
            """,
            (tok,),
        )
        last = c.fetchone()
        return {
            "native_volume": int(native_volume or 0),
            "token_volume": int(token_volume or 0),
            "volume_usd": Decimal(volume_usd or 0),
            "tx_count": int(tx_count or 0),
            "buy_count": int(buys or 0),
            "sell_count": int(sells or 0),
            "last_price_native": (last[0] if last else None),
            "native_reserve": int(last[1] or 0) if last else 0,
            "token_reserve": int(last[2] or 0) if last else 0,
        }

    if cur is None:
        with db_cursor() as cur2:
            return _run(cur2)
    return _run(cur)


# true when a trade with this txhash and log index is already stored
def trade_exists(txhash: str, log_index: int, cur: psycopg2.extensions.cursor | None = None) -> bool:
    tx = (txhash or "").lower()
    if not tx:
        return False
    sql = "SELECT 1 FROM launchpad_trades WHERE txhash = %s AND log_index = %s LIMIT 1;"
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, (tx, int(log_index)))
            return cur2.fetchone() is not None
    cur.execute(sql, (tx, int(log_index)))
    return cur.fetchone() is not None


# write a tokens aggregate state after a trade, raising its ath
def update_token_after_trade(
    *,
    token: str,
    last_price_native,
    native_volume,
    token_volume,
    volume_usd,
    fees_usd,
    buy_count: int,
    sell_count: int,
    tx_count: int,
    circulating_supply,
    approaching_75: bool,
    approaching_75_block: int,
    approaching_75_at: int,
    snipers_count: int,
    curve_native_reserve=0,
    curve_token_reserve=0,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                UPDATE launchpad_tokens
                SET
                    last_price_native = %s,
                    native_volume = %s,
                    token_volume = %s,
                    volume_usd = %s,
                    fees_usd = %s,
                    buy_count = %s,
                    sell_count = %s,
                    tx_count = %s,
                    circulating_supply = %s,
                    approaching_75 = %s,
                    approaching_75_block = %s,
                    approaching_75_at = %s,
                    snipers_count = %s,
                    curve_native_reserve = %s,
                    curve_token_reserve = %s,
                    ath_price_native = GREATEST(launchpad_tokens.ath_price_native, %s)
                WHERE token = %s;
                """,
                (
                    last_price_native,
                    int(native_volume),
                    int(token_volume),
                    volume_usd,
                    fees_usd,
                    int(buy_count),
                    int(sell_count),
                    int(tx_count),
                    circulating_supply,
                    bool(approaching_75),
                    int(approaching_75_block) if approaching_75_block is not None else None,
                    int(approaching_75_at) if approaching_75_at is not None else None,
                    int(snipers_count),
                    int(curve_native_reserve or 0),
                    int(curve_token_reserve or 0),
                    last_price_native,
                    token.lower(),
                ),
            )
    else:
        cur.execute(
            """
            UPDATE launchpad_tokens
            SET
                last_price_native = %s,
                native_volume = %s,
                token_volume = %s,
                volume_usd = %s,
                fees_usd = %s,
                buy_count = %s,
                sell_count = %s,
                tx_count = %s,
                circulating_supply = %s,
                approaching_75 = %s,
                approaching_75_block = %s,
                approaching_75_at = %s,
                snipers_count = %s,
                curve_native_reserve = %s,
                curve_token_reserve = %s,
                ath_price_native = GREATEST(launchpad_tokens.ath_price_native, %s)
            WHERE token = %s;
            """,
            (
                last_price_native,
                int(native_volume),
                int(token_volume),
                volume_usd,
                fees_usd,
                int(buy_count),
                int(sell_count),
                int(tx_count),
                circulating_supply,
                bool(approaching_75),
                int(approaching_75_block) if approaching_75_block is not None else None,
                int(approaching_75_at) if approaching_75_at is not None else None,
                int(snipers_count),
                int(curve_native_reserve or 0),
                int(curve_token_reserve or 0),
                last_price_native,
                token.lower(),
            ),
        )


# accumulate one users volume trade count and realized pnl
def update_user_on_trade(
    *, address: str, native_amount: int, realized_delta, cur: psycopg2.extensions.cursor | None = None
) -> None:
    addr = address.lower()
    if not addr:
        return

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_users (
                    address,
                    total_native_volume,
                    total_realized_pnl_native,
                    total_trades
                )
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (address) DO UPDATE
                SET
                    total_native_volume = launchpad_users.total_native_volume + EXCLUDED.total_native_volume,
                    total_realized_pnl_native = launchpad_users.total_realized_pnl_native + EXCLUDED.total_realized_pnl_native,
                    total_trades = launchpad_users.total_trades + EXCLUDED.total_trades;
                """,
                (
                    addr,
                    int(abs(native_amount)),
                    realized_delta,
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_users (
                address,
                total_native_volume,
                total_realized_pnl_native,
                total_trades
            )
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (address) DO UPDATE
            SET
                total_native_volume = launchpad_users.total_native_volume + EXCLUDED.total_native_volume,
                total_realized_pnl_native = launchpad_users.total_realized_pnl_native + EXCLUDED.total_realized_pnl_native,
                total_trades = launchpad_users.total_trades + EXCLUDED.total_trades;
            """,
            (
                addr,
                int(abs(native_amount)),
                realized_delta,
            ),
        )


# open token count and cost basis for one position, zeros when it does not exist
def get_position_basis(user_address: str, token: str, cur=None) -> tuple[int, int]:
    addr = (user_address or "").lower()
    tok = (token or "").lower()
    if not addr or not tok:
        return 0, 0

    def _run(c):
        c.execute(
            """
            SELECT token_bought, token_sold, cost_basis_native
            FROM launchpad_positions
            WHERE user_address = %s AND token = %s
            """,
            (addr, tok),
        )
        row = c.fetchone()
        if not row:
            return 0, 0
        open_tokens = int(row[0] or 0) - int(row[1] or 0)
        return max(open_tokens, 0), int(row[2] or 0)

    if cur is None:
        with db_cursor() as c2:
            return _run(c2)
    return _run(cur)


# apply one position delta for a user and token
def upsert_position(
    *,
    user_address: str,
    token: str,
    token_bought_delta: int,
    token_sold_delta: int,
    native_spent_delta: int,
    native_received_delta: int,
    balance_token_delta: int,
    realized_pnl_delta,
    trade_count_delta: int,
    buy_count_delta: int,
    sell_count_delta: int,
    last_price_native,
    cost_basis_delta: int = 0,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    addr = user_address.lower()
    tok = token.lower()
    if not addr or not tok:
        return

    tb = int(token_bought_delta)
    ts = int(token_sold_delta)
    ns = int(native_spent_delta)
    nr = int(native_received_delta)
    bd = int(balance_token_delta)
    tc = int(trade_count_delta)
    bc = int(buy_count_delta)
    sc = int(sell_count_delta)

    cb = int(cost_basis_delta)
    balance_insert = max(bd, 0)
    unrealized_insert = Decimal(balance_insert) * Decimal(last_price_native)
    total_insert = Decimal(realized_pnl_delta) + unrealized_insert

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_positions (
                    user_address,
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
                    cost_basis_native
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_address, token) DO UPDATE
                SET
                    token_bought = launchpad_positions.token_bought + EXCLUDED.token_bought,
                    token_sold = launchpad_positions.token_sold + EXCLUDED.token_sold,
                    native_spent = launchpad_positions.native_spent + EXCLUDED.native_spent,
                    native_received = launchpad_positions.native_received + EXCLUDED.native_received,
                    balance_token = GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0),
                    realized_pnl_native = launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native,
                    trade_count = launchpad_positions.trade_count + EXCLUDED.trade_count,
                    buy_count = launchpad_positions.buy_count + EXCLUDED.buy_count,
                    sell_count = launchpad_positions.sell_count + EXCLUDED.sell_count,
                    cost_basis_native = GREATEST(launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native, 0),
                    unrealized_pnl_native = GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0) * %s,
                    total_pnl_native = (
                        launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native
                    ) + GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0) * %s;
                """,
                (
                    addr,
                    tok,
                    tb,
                    ts,
                    ns,
                    nr,
                    bd,
                    realized_pnl_delta,
                    unrealized_insert,
                    total_insert,
                    tc,
                    bc,
                    sc,
                    cb,
                    last_price_native,
                    last_price_native,
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_positions (
                user_address,
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
                cost_basis_native
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_address, token) DO UPDATE
            SET
                token_bought = launchpad_positions.token_bought + EXCLUDED.token_bought,
                token_sold = launchpad_positions.token_sold + EXCLUDED.token_sold,
                native_spent = launchpad_positions.native_spent + EXCLUDED.native_spent,
                native_received = launchpad_positions.native_received + EXCLUDED.native_received,
                balance_token = GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0),
                realized_pnl_native = launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native,
                trade_count = launchpad_positions.trade_count + EXCLUDED.trade_count,
                buy_count = launchpad_positions.buy_count + EXCLUDED.buy_count,
                sell_count = launchpad_positions.sell_count + EXCLUDED.sell_count,
                cost_basis_native = GREATEST(launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native, 0),
                unrealized_pnl_native = GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0) * %s,
                total_pnl_native = (
                    launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native
                ) + GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0) * %s;
            """,
            (
                addr,
                tok,
                tb,
                ts,
                ns,
                nr,
                bd,
                realized_pnl_delta,
                unrealized_insert,
                total_insert,
                tc,
                bc,
                sc,
                cb,
                last_price_native,
                last_price_native,
            ),
        )


# open high low close and volume for one bucket
def upsert_ohlcv(
    *,
    token: str,
    resolution_sec: int,
    bucket_start: int,
    price_native,
    native_amount: int,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_ohlcv (
                    token,
                    resolution_sec,
                    bucket_start,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    quote_volume
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (token, resolution_sec, bucket_start) DO UPDATE
                SET
                    high_price = GREATEST(launchpad_ohlcv.high_price, EXCLUDED.high_price),
                    low_price = LEAST(launchpad_ohlcv.low_price, EXCLUDED.low_price),
                    close_price = EXCLUDED.close_price,
                    quote_volume = launchpad_ohlcv.quote_volume + EXCLUDED.quote_volume;
                """,
                (
                    token.lower(),
                    int(resolution_sec),
                    int(bucket_start),
                    price_native,
                    price_native,
                    price_native,
                    price_native,
                    int(abs(native_amount)),
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_ohlcv (
                token,
                resolution_sec,
                bucket_start,
                open_price,
                high_price,
                low_price,
                close_price,
                quote_volume
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (token, resolution_sec, bucket_start) DO UPDATE
            SET
                high_price = GREATEST(launchpad_ohlcv.high_price, EXCLUDED.high_price),
                low_price = LEAST(launchpad_ohlcv.low_price, EXCLUDED.low_price),
                close_price = EXCLUDED.close_price,
                quote_volume = launchpad_ohlcv.quote_volume + EXCLUDED.quote_volume;
            """,
            (
                token.lower(),
                int(resolution_sec),
                int(bucket_start),
                price_native,
                price_native,
                price_native,
                price_native,
                int(abs(native_amount)),
            ),
        )


# record a sniper, returning whether the row was new
def add_sniper_address(token: str, user_address: str, cur: psycopg2.extensions.cursor | None = None) -> bool:
    tok = token.lower()
    addr = user_address.lower()
    if not tok or not addr:
        return False

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_snipers (token, user_address)
                VALUES (%s, %s)
                ON CONFLICT (token, user_address) DO NOTHING;
                """,
                (tok, addr),
            )
            inserted = cur2.rowcount == 1
            if inserted:
                cur2.execute(
                    """
                    UPDATE launchpad_tokens
                    SET snipers_count = snipers_count + 1
                    WHERE token = %s;
                    """,
                    (tok,),
                )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_snipers (token, user_address)
            VALUES (%s, %s)
            ON CONFLICT (token, user_address) DO NOTHING;
            """,
            (tok, addr),
        )
        inserted = cur.rowcount == 1
        if inserted:
            cur.execute(
                """
                UPDATE launchpad_tokens
                SET snipers_count = snipers_count + 1
                WHERE token = %s;
                """,
                (tok,),
            )

    return inserted


# insert or update a token row from a tokencreated event
def upsert_token_created(
    *,
    token: str,
    creator: str,
    name: str,
    symbol: str,
    metadata_cid: str,
    description: str,
    social1: str,
    social2: str,
    social3: str,
    social4: str,
    source: int,
    created_block: int,
    created_at: int,
    last_price_native,
    quote_token: str | None = None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    quote_token_l = (quote_token or WMON).lower()
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_tokens (
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
                    last_price_native,
                    quote_token
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE
                SET
                    -- the nad.fun create event carries empty strings for everything
                    -- the metadata worker fills in later, so overwriting on replay
                    -- wiped the image, description and socials off the token
                    creator = COALESCE(NULLIF(EXCLUDED.creator, ''), launchpad_tokens.creator),
                    name = COALESCE(NULLIF(EXCLUDED.name, ''), launchpad_tokens.name),
                    symbol = COALESCE(NULLIF(EXCLUDED.symbol, ''), launchpad_tokens.symbol),
                    metadata_cid = COALESCE(NULLIF(EXCLUDED.metadata_cid, ''), launchpad_tokens.metadata_cid),
                    description = COALESCE(NULLIF(EXCLUDED.description, ''), launchpad_tokens.description),
                    social1 = COALESCE(NULLIF(EXCLUDED.social1, ''), launchpad_tokens.social1),
                    social2 = COALESCE(NULLIF(EXCLUDED.social2, ''), launchpad_tokens.social2),
                    social3 = COALESCE(NULLIF(EXCLUDED.social3, ''), launchpad_tokens.social3),
                    social4 = COALESCE(NULLIF(EXCLUDED.social4, ''), launchpad_tokens.social4),
                    source = EXCLUDED.source,
                    created_block = EXCLUDED.created_block,
                    created_at = EXCLUDED.created_at,
                    last_price_native = EXCLUDED.last_price_native,
                    quote_token = EXCLUDED.quote_token;
                """,
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
                    int(source),
                    int(created_block),
                    int(created_at),
                    last_price_native,
                    quote_token_l,
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_tokens (
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
                last_price_native,
                quote_token
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (token) DO UPDATE
            SET
                -- same guard as the pooled variant: an empty value from the create
                -- event must never overwrite what the metadata worker filled in
                creator = COALESCE(NULLIF(EXCLUDED.creator, ''), launchpad_tokens.creator),
                name = COALESCE(NULLIF(EXCLUDED.name, ''), launchpad_tokens.name),
                symbol = COALESCE(NULLIF(EXCLUDED.symbol, ''), launchpad_tokens.symbol),
                metadata_cid = COALESCE(NULLIF(EXCLUDED.metadata_cid, ''), launchpad_tokens.metadata_cid),
                description = COALESCE(NULLIF(EXCLUDED.description, ''), launchpad_tokens.description),
                social1 = COALESCE(NULLIF(EXCLUDED.social1, ''), launchpad_tokens.social1),
                social2 = COALESCE(NULLIF(EXCLUDED.social2, ''), launchpad_tokens.social2),
                social3 = COALESCE(NULLIF(EXCLUDED.social3, ''), launchpad_tokens.social3),
                social4 = COALESCE(NULLIF(EXCLUDED.social4, ''), launchpad_tokens.social4),
                source = EXCLUDED.source,
                created_block = EXCLUDED.created_block,
                created_at = EXCLUDED.created_at,
                last_price_native = EXCLUDED.last_price_native,
                quote_token = EXCLUDED.quote_token;
            """,
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
                int(source),
                int(created_block),
                int(created_at),
                last_price_native,
                quote_token_l,
            ),
        )


# bump a creators launched token count
def increment_user_tokens_created(address: str, cur: psycopg2.extensions.cursor | None = None) -> None:
    addr = address.lower()
    if not addr:
        return

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_users (address, tokens_created)
                VALUES (%s, 1)
                ON CONFLICT (address) DO UPDATE
                SET tokens_created = launchpad_users.tokens_created + 1;
                """,
                (addr,),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_users (address, tokens_created)
            VALUES (%s, 1)
            ON CONFLICT (address) DO UPDATE
            SET tokens_created = launchpad_users.tokens_created + 1;
            """,
            (addr,),
        )


# flag a token as migrated, keeping any market already linked
def mark_token_migrated(
    *,
    token: str,
    migrated_block: int,
    migrated_at: int,
    pool: str | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    tok = token.lower()
    pool_addr = (pool or "").lower() or None

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                UPDATE launchpad_tokens
                SET
                    migrated = TRUE,
                    migrated_block = %s,
                    migrated_at = %s,
                    market = COALESCE(%s, market)
                WHERE token = %s;
                """,
                (int(migrated_block), int(migrated_at), pool_addr, tok),
            )
    else:
        cur.execute(
            """
            UPDATE launchpad_tokens
            SET
                migrated = TRUE,
                migrated_block = %s,
                migrated_at = %s,
                market = COALESCE(%s, market)
            WHERE token = %s;
            """,
            (int(migrated_block), int(migrated_at), pool_addr, tok),
        )


# link a graduated token to its market
def update_launchpad_token_market(
    *,
    token: str,
    market: str,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    tok = (token or "").lower()
    mkt = (market or "").lower()
    if not tok or not mkt:
        return
    sql = """
        UPDATE launchpad_tokens
        SET market = %s
        WHERE token = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, (mkt, tok))
    else:
        cur.execute(sql, (mkt, tok))


# write the latest price for a token
def update_launchpad_token_price(
    *,
    token: str,
    last_price_native,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    tok = (token or "").lower()
    if not tok:
        return
    sql = """
        UPDATE launchpad_tokens
        SET last_price_native = %s
        WHERE token = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, (last_price_native, tok))
    else:
        cur.execute(sql, (last_price_native, tok))


# apply fetched metadata to many tokens at once
def update_token_metadata_batch(metadata_list: list[dict]) -> None:
    if not metadata_list:
        return
    with db_cursor() as cur:
        for meta in metadata_list:
            token = meta.get("token", "").lower()
            if not token:
                continue
            cur.execute(
                """
                UPDATE launchpad_tokens
                SET
                    description = COALESCE(NULLIF(%s, ''), description),
                    metadata_cid = COALESCE(NULLIF(%s, ''), metadata_cid),
                    social1 = COALESCE(NULLIF(%s, ''), social1),
                    social2 = COALESCE(NULLIF(%s, ''), social2),
                    social3 = COALESCE(NULLIF(%s, ''), social3)
                WHERE token = %s;
                """,
                (
                    meta.get("description", ""),
                    meta.get("image_uri", ""),
                    meta.get("website", ""),
                    meta.get("twitter", ""),
                    meta.get("telegram", ""),
                    token,
                ),
            )


# bump a creators graduated token count
def increment_user_tokens_graduated(address: str, cur: psycopg2.extensions.cursor | None = None) -> None:
    addr = address.lower()
    if not addr:
        return

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_users (address, tokens_graduated)
                VALUES (%s, 1)
                ON CONFLICT (address) DO UPDATE
                SET tokens_graduated = launchpad_users.tokens_graduated + 1;
                """,
                (addr,),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_users (address, tokens_graduated)
            VALUES (%s, 1)
            ON CONFLICT (address) DO UPDATE
            SET tokens_graduated = launchpad_users.tokens_graduated + 1;
            """,
            (addr,),
        )


# insert or update one v2 or v3 pool
def upsert_pool(
    *,
    pool: str,
    token_addr: str,
    native_addr: str,
    token_is_0: bool,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_pools (
                    pool,
                    token_addr,
                    native_addr,
                    token_is_0
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (pool) DO UPDATE
                SET
                    token_addr = EXCLUDED.token_addr,
                    native_addr = EXCLUDED.native_addr,
                    token_is_0 = EXCLUDED.token_is_0;
                """,
                (
                    pool.lower(),
                    token_addr.lower(),
                    native_addr.lower(),
                    bool(token_is_0),
                ),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_pools (
                pool,
                token_addr,
                native_addr,
                token_is_0
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (pool) DO UPDATE
            SET
                token_addr = EXCLUDED.token_addr,
                native_addr = EXCLUDED.native_addr,
                token_is_0 = EXCLUDED.token_is_0;
            """,
            (
                pool.lower(),
                token_addr.lower(),
                native_addr.lower(),
                bool(token_is_0),
            ),
        )


# every known pool for state rebuild
def load_all_pools():
    with db_cursor() as cur:
        cur.execute("""
            SELECT pool, token_addr, native_addr, token_is_0
            FROM launchpad_pools
        """)
        return cur.fetchall()


# every launchpad token for state rebuild
def load_tokens_for_state():
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
                quote_token,
                curve_native_reserve,
                curve_token_reserve
            FROM launchpad_tokens
            """
        )
        return cur.fetchall()


# name and symbol search over launchpad tokens
def search_tokens(query: str, limit: int = 20):
    q = (query or "").strip().lower()
    if not q:
        return []

    prefix = q + "%"
    contains = "%" + q + "%"

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                token,
                circulating_supply,
                (
                    CASE WHEN LOWER(symbol) = %s THEN 100 ELSE 0 END +
                    CASE WHEN LOWER(name) = %s THEN 90 ELSE 0 END +
                    CASE WHEN LOWER(token) = %s THEN 80 ELSE 0 END +

                    CASE WHEN LOWER(symbol) LIKE %s THEN 60 ELSE 0 END +
                    CASE WHEN LOWER(name) LIKE %s THEN 50 ELSE 0 END +
                    CASE WHEN LOWER(token) LIKE %s THEN 40 ELSE 0 END +

                    CASE WHEN LOWER(symbol) LIKE %s THEN 30 ELSE 0 END +
                    CASE WHEN LOWER(name) LIKE %s THEN 20 ELSE 0 END +
                    CASE WHEN LOWER(token) LIKE %s THEN 10 ELSE 0 END +

                    similarity(symbol, %s) * 10 +
                    similarity(name, %s) * 10 +
                    similarity(token, %s) * 10
                ) AS score
            FROM launchpad_tokens
            WHERE
                symbol ILIKE %s OR
                name ILIKE %s OR
                token ILIKE %s OR
                similarity(symbol, %s) > 0.1 OR
                similarity(name, %s) > 0.1 OR
                similarity(token, %s) > 0.1
            ORDER BY score DESC, created_at DESC
            LIMIT %s;
            """,
            (
                q,
                q,
                q,
                prefix,
                prefix,
                prefix,
                contains,
                contains,
                contains,
                q,
                q,
                q,
                contains,
                contains,
                contains,
                q,
                q,
                q,
                limit,
            ),
        )
        return cur.fetchall()


# a social slot reduced to a bare hostname: scheme and a leading www. removed, path
# and query dropped. matches the normalisation the client applies to a blacklist entry
def _host_expr(col: str) -> str:
    return (
        "regexp_replace("
        f"split_part(split_part(lower(regexp_replace(COALESCE({col}, ''), '^https?://', '', 'i')), '/', 1), '?', 1)"
        r", '^www\.', '')"
    )


# a social slot reduced to a bare twitter handle, or null when the slot is not twitter.
# returning null rather than the raw value keeps a website from matching a handle
def _handle_expr(col: str) -> str:
    tw = r"'^(https?://)?(www\.)?(x|twitter)\.com/'"
    return (
        "CASE "
        f"WHEN COALESCE({col}, '') ~* {tw} THEN "
        f"lower(split_part(split_part(regexp_replace({col}, {tw}, '', 'i'), '/', 1), '?', 1)) "
        f"WHEN COALESCE({col}, '') ~ '^@' THEN lower(substring({col} from 2)) "
        "ELSE NULL END"
    )


# blacklist exclusions as a sql fragment plus params, shared by every list endpoint.
# built once so /tokens and /search/query cannot drift into normalising differently
def blacklist_clauses(ex: dict | None, alias: str = "t") -> tuple[list[str], list]:
    ex = ex or {}
    a = f"{alias}." if alias else ""
    where: list[str] = []
    params: list = []

    def norm(key):
        return [str(v).strip().lower() for v in (ex.get(key) or []) if str(v).strip()]

    # the column expressions produce a bare host and a bare handle, so the incoming
    # values have to be reduced the same way. the client already sends them bare, but
    # a full url arriving here would otherwise silently match nothing
    def norm_host(key):
        out = []
        for v in norm(key):
            v = re.sub(r"^https?://", "", v)
            v = re.sub(r"^www\.", "", v)
            out.append(v.split("/")[0].split("?")[0])
        return [v for v in out if v]

    def norm_handle(key):
        out = []
        for v in norm(key):
            v = re.sub(r"^(https?://)?(www\.)?(x|twitter)\.com/", "", v)
            v = v.lstrip("@")
            out.append(v.split("/")[0].split("?")[0])
        return [v for v in out if v]

    dev = norm("exclude_dev")
    if dev:
        where.append(f"LOWER(COALESCE({a}creator, '')) <> ALL(%s)")
        params.append(dev)

    ca = norm("exclude_ca")
    if ca:
        where.append(f"LOWER({a}token) <> ALL(%s)")
        params.append(ca)

    for key, build, prep in (
        ("exclude_website", _host_expr, norm_host),
        ("exclude_twitter", _handle_expr, norm_handle),
    ):
        vals = prep(key)
        if not vals:
            continue
        # coalesce is load bearing: the handle expression is null for a slot that is
        # not twitter, and NOT (null OR false) is null, which drops the row. without
        # it a single twitter entry emptied the whole feed
        clauses = [f"COALESCE({build(f'{a}social{i}')}, '')" for i in range(1, 5)]
        where.append("NOT (" + " OR ".join(f"({c}) = ANY(%s)" for c in clauses) + ")")
        params.extend([vals] * len(clauses))

    return where, params


# filtered token search. every filter is applied in SQL rather than to an already
# truncated page, because filtering the top 50 silently hides a token that matches
# but ranks 51st -- results that are wrong rather than merely incomplete
def search_tokens_filtered(
    *,
    query: str = "",
    filters: dict | None = None,
    sort: str = "",
    limit: int = 50,
    offset: int = 0,
    mon_usd=None,
) -> tuple[list[str], dict, int]:
    f = filters or {}
    q = (query or "").strip().lower()
    mon = Decimal(str(mon_usd or 0))

    where: list[str] = []
    params: list = []

    if q:
        like = f"%{q}%"
        where.append("(LOWER(t.symbol) LIKE %s OR LOWER(t.name) LIKE %s OR LOWER(t.token) LIKE %s)")
        params += [like, like, like]

    # phase is derived from migration plus the curve flag, not a stored column
    phase = (f.get("phase") or "").strip().lower()
    if phase == "graduated":
        where.append("t.migrated = TRUE")
    elif phase == "graduating":
        where.append("t.migrated = FALSE AND t.approaching_75 = TRUE")
    elif phase == "new":
        where.append("t.migrated = FALSE AND t.approaching_75 = FALSE")

    def rng(col: str, lo_key: str, hi_key: str) -> None:
        lo, hi = f.get(lo_key), f.get(hi_key)
        if lo is not None:
            where.append(f"{col} >= %s")
            params.append(Decimal(str(lo)))
        if hi is not None:
            where.append(f"{col} <= %s")
            params.append(Decimal(str(hi)))

    # age arrives in minutes: a larger age_min means an older token, so a smaller ts
    now = int(time.time())
    if f.get("age_min") is not None:
        where.append("t.created_at <= %s")
        params.append(now - int(float(f["age_min"]) * 60))
    if f.get("age_max") is not None:
        where.append("t.created_at >= %s")
        params.append(now - int(float(f["age_max"]) * 60))

    # nad.fun v1 and v2 are both reported as source 1 on the wire, so a request for
    # nad.fun has to match either generation
    src = f.get("source")
    if src is not None and str(src).strip() != "":
        if int(src) == 0:
            where.append("t.source = 0")
        else:
            where.append("t.source <> 0")

    # blacklist exclusions run in sql for the same reason the filters do: applied to
    # the current page they hide what happens to be on screen and miss every other
    # match, which is a promise the feature cannot keep
    bl_where, bl_params = blacklist_clauses(f, "t")
    where.extend(bl_where)
    params.extend(bl_params)

    rng("t.volume_usd", "volume_24h_min", "volume_24h_max")
    rng("t.fees_usd", "fees_min", "fees_max")
    rng("t.buy_count", "buy_tx_min", "buy_tx_max")
    rng("t.sell_count", "sell_tx_min", "sell_tx_max")
    rng("t.last_price_native", "price_min", "price_max")

    # marketcap is price x total supply x the live mon price
    if mon > 0:
        for key, op in (("marketcap_min", ">="), ("marketcap_max", "<=")):
            v = f.get(key)
            if v is not None:
                where.append(f"(t.last_price_native * 1000000000 * %s) {op} %s")
                params += [mon, Decimal(str(v))]

    for key, negate in (("keywords", False), ("exclude_keywords", True)):
        terms = [w.strip().lower() for w in (f.get(key) or "").split(",") if w.strip()]
        if not terms:
            continue
        clause = " OR ".join(["(LOWER(t.name) LIKE %s OR LOWER(t.symbol) LIKE %s)"] * len(terms))
        where.append(f"{'NOT ' if negate else ''}({clause})")
        for term in terms:
            params += [f"%{term}%", f"%{term}%"]

    # socials are matched by host across all four slots, because slot order is not
    # guaranteed -- social1 is not reliably twitter
    cols = ("t.social1", "t.social2", "t.social3", "t.social4")
    for key, hosts in (
        ("has_twitter", ("%x.com/%", "%twitter.com/%")),
        ("has_telegram", ("%t.me/%",)),
        ("has_discord", ("%discord.gg/%", "%discord.com/%")),
    ):
        if not f.get(key):
            continue
        ors = []
        for c in cols:
            for h in hosts:
                ors.append(f"LOWER({c}) LIKE %s")
                params.append(h)
        where.append("(" + " OR ".join(ors) + ")")

    if f.get("has_website"):
        ors = [f"({c} IS NOT NULL AND {c} <> '' AND LOWER({c}) LIKE %s)" for c in cols]
        where.append("(" + " OR ".join(ors) + ")")
        params += ["%.%"] * len(cols)

    where_sql = " AND ".join(where) if where else "TRUE"

    # holder derived metrics need launchpad_positions, so they are computed per
    # candidate row and filtered in the outer query
    derived: list[str] = []
    dparams: list = []

    def drng(col: str, lo_key: str, hi_key: str) -> None:
        lo, hi = f.get(lo_key), f.get(hi_key)
        if lo is not None:
            derived.append(f"{col} >= %s")
            dparams.append(Decimal(str(lo)))
        if hi is not None:
            derived.append(f"{col} <= %s")
            dparams.append(Decimal(str(hi)))

    drng("b.holders", "holders_min", "holders_max")
    drng("b.dev_pct", "dev_holding_min", "dev_holding_max")
    drng("b.top10_pct", "top10_min", "top10_max")
    drng("b.sniper_pct", "sniper_holding_min", "sniper_holding_max")
    drng("b.pro_traders", "pro_traders_min", "pro_traders_max")
    drng("b.insider_pct", "insider_holding_min", "insider_holding_max")
    derived_sql = " AND ".join(derived) if derived else "TRUE"

    order = {
        "mc": "b.last_price_native DESC",
        "volume_24h": "b.volume_usd DESC",
        "volume_1h": "b.volume_usd DESC",
        "holders": "b.holders DESC",
        "recent": "b.created_at DESC",
    }.get((sort or "").lower(), "b.created_at DESC")

    # percentages are of the 1e27 total supply, so raw balance / 1e25. the holder
    # derived numbers come from ONE pass over launchpad_positions grouped by token,
    # joined onto the candidates: the per row correlated subqueries ran six index
    # probes per candidate, which at 31k candidates was a seven second query
    base = f"""
        WITH agg AS (
            SELECT token,
                   COUNT(*) FILTER (WHERE balance_token > 1) AS holders,
                   COUNT(*) FILTER (WHERE realized_pnl_native > 0 AND trade_count >= 10) AS pro_traders,
                   COALESCE(SUM(balance_token)
                       FILTER (WHERE balance_token > (token_bought - token_sold) + 1e18), 0) / 1e25 AS insider_pct
            FROM launchpad_positions GROUP BY token
        ),
        top10 AS (
            -- the window only ever matters for positive balances, and most position
            -- rows are sold out zeros, so sorting only the live ones keeps this cheap
            SELECT token, COALESCE(SUM(balance_token), 0) / 1e25 AS top10_pct
            FROM (
                SELECT p.token, p.balance_token,
                       ROW_NUMBER() OVER (PARTITION BY p.token ORDER BY p.balance_token DESC) AS rn
                FROM launchpad_positions p WHERE p.balance_token > 0
            ) x WHERE rn <= 10 GROUP BY token
        ),
        dev AS (
            SELECT p.token, COALESCE(SUM(p.balance_token), 0) / 1e25 AS dev_pct
            FROM launchpad_positions p
            JOIN launchpad_tokens tt ON tt.token = p.token AND p.user_address = tt.creator
            GROUP BY p.token
        ),
        snip AS (
            SELECT p.token, COALESCE(SUM(p.balance_token), 0) / 1e25 AS sniper_pct
            FROM launchpad_positions p
            JOIN launchpad_snipers s ON s.token = p.token AND s.user_address = p.user_address
            GROUP BY p.token
        )
        SELECT t.token, t.circulating_supply, t.created_at, t.volume_usd,
               t.last_price_native,
               COALESCE(a.holders, 0) AS holders,
               COALESCE(d.dev_pct, 0) AS dev_pct,
               COALESCE(tp.top10_pct, 0) AS top10_pct,
               COALESCE(sn.sniper_pct, 0) AS sniper_pct,
               COALESCE(a.pro_traders, 0) AS pro_traders,
               COALESCE(a.insider_pct, 0) AS insider_pct
        FROM launchpad_tokens t
        LEFT JOIN agg a ON a.token = t.token
        LEFT JOIN top10 tp ON tp.token = t.token
        LEFT JOIN dev d ON d.token = t.token
        LEFT JOIN snip sn ON sn.token = t.token
        WHERE {where_sql}
    """

    with db_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM ({base}) b WHERE {derived_sql}",
            tuple(params) + tuple(dparams),
        )
        row = cur.fetchone()
        total = int(row[0]) if row else 0

        cur.execute(
            f"""
            SELECT b.token, b.circulating_supply FROM ({base}) b
            WHERE {derived_sql}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            tuple(params) + tuple(dparams) + (int(limit), int(offset)),
        )
        rows = cur.fetchall()

    tokens = [(r[0] or "").lower() for r in rows]
    circ = {(r[0] or "").lower(): r[1] for r in rows}
    return tokens, circ, total


# persist the latest mon usd price
def set_mon_price_usd(value) -> None:
    val = Decimal(value)
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_meta (key, value)
            VALUES ('mon_price_usd', %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value;
            """,
            (val,),
        )


# last persisted mon usd price
def get_mon_price_usd():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT value
            FROM launchpad_meta
            WHERE key = 'mon_price_usd';
            """
        )
        row = cur.fetchone()
    return row[0] if row else None


# addresses excluded from holder counts
def load_holder_denylist() -> list[str]:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT address FROM holder_denylist")
            return [r[0].lower() for r in cur.fetchall() if r[0]]
    except Exception:
        return []


# flag a nadfun token as being on the v2 curve
def mark_nadfun_v2(token: str, cur: psycopg2.extensions.cursor | None = None) -> None:
    tok = (token or "").lower()
    if not tok:
        return
    sql = "INSERT INTO nadfun_v2_tokens (token) VALUES (%s) ON CONFLICT (token) DO NOTHING"
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, (tok,))
    else:
        cur.execute(sql, (tok,))


# every nadfun token known to be on v2
def load_nadfun_v2_tokens() -> list[str]:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT token FROM nadfun_v2_tokens")
            return [r[0].lower() for r in cur.fetchall() if r[0]]
    except Exception:
        return []


# zero one users position in a token
def clear_position(
    *,
    user_address: str,
    token: str,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    addr = user_address.lower()
    tok = token.lower()
    if not addr or not tok:
        return

    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                DELETE FROM launchpad_positions
                WHERE user_address = %s AND token = %s;
                """,
                (addr, tok),
            )
    else:
        cur.execute(
            """
            DELETE FROM launchpad_positions
            WHERE user_address = %s AND token = %s;
            """,
            (addr, tok),
        )


# cache one blocks raw logs for replay
def write_block_logs(block_number: int, logs: list[dict], cur: psycopg2.extensions.cursor | None = None) -> None:
    if not logs:
        logs = []
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO launchpad_block_logs (number, logs)
                VALUES (%s, %s)
                ON CONFLICT (number) DO NOTHING
                """,
                (block_number, Json(logs)),
            )
    else:
        cur.execute(
            """
            INSERT INTO launchpad_block_logs (number, logs)
            VALUES (%s, %s)
            ON CONFLICT (number) DO NOTHING
            """,
            (block_number, Json(logs)),
        )


# cache raw logs for many blocks at once
def write_block_logs_batch(blocks: dict[int, list[dict]], cur) -> None:
    if not blocks:
        return
    data = [(blk, Json(logs or [])) for blk, logs in blocks.items()]
    cur.executemany(
        """
        INSERT INTO launchpad_block_logs (number, logs)
        VALUES (%s, %s)
        ON CONFLICT (number) DO NOTHING
        """,
        data,
    )


# cached raw logs for one block
def get_block_logs(block_number: int) -> list[dict] | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT logs
            FROM launchpad_block_logs
            WHERE number = %s
            """,
            (block_number,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row[0] or []


# cached raw logs for a block range
def get_block_logs_range(start_block: int, end_block: int, cur=None) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(
                """
                SELECT number, logs
                FROM launchpad_block_logs
                WHERE number BETWEEN %s AND %s
                """,
                (start_block, end_block),
            )
            for num, logs in cur2.fetchall():
                result[int(num)] = logs or []
    else:
        cur.execute(
            """
            SELECT number, logs
            FROM launchpad_block_logs
            WHERE number BETWEEN %s AND %s
            """,
            (start_block, end_block),
        )
        for num, logs in cur.fetchall():
            result[int(num)] = logs or []
    return result


# write many trade rows in one statement
def insert_trades_batch(trades: list[tuple], cur) -> None:
    if not trades:
        return
    execute_values(
        cur,
        """
        INSERT INTO launchpad_trades (
            block_number, log_index, timestamp, token, user_address,
            is_buy, native_amount, token_amount, usd_amount, price_native, txhash,
            native_reserve, token_reserve
        )
        VALUES %s
        ON CONFLICT (txhash, log_index) DO NOTHING
        """,
        trades,
        page_size=1000,
    )


# write many token aggregate updates in one statement
def update_tokens_batch(token_updates: dict[str, dict], cur) -> None:
    if not token_updates:
        return
    data = []
    for token, u in token_updates.items():
        data.append(
            (
                u["last_price_native"],
                int(u["native_volume"]),
                int(u["token_volume"]),
                u["volume_usd"],
                u["fees_usd"],
                int(u["buy_count"]),
                int(u["sell_count"]),
                int(u["tx_count"]),
                u["circulating_supply"],
                bool(u["approaching_75"]),
                int(u["approaching_75_block"]) if u.get("approaching_75_block") else None,
                int(u["approaching_75_at"]) if u.get("approaching_75_at") else None,
                int(u["snipers_count"]),
                int(u.get("curve_native_reserve") or 0),
                int(u.get("curve_token_reserve") or 0),
                u["last_price_native"],
                token.lower(),
            )
        )
    execute_values(
        cur,
        """
        UPDATE launchpad_tokens AS t SET
            last_price_native = v.last_price_native,
            native_volume = v.native_volume,
            token_volume = v.token_volume,
            volume_usd = v.volume_usd,
            fees_usd = v.fees_usd,
            buy_count = v.buy_count,
            sell_count = v.sell_count,
            tx_count = v.tx_count,
            circulating_supply = v.circulating_supply,
            approaching_75 = v.approaching_75,
            approaching_75_block = v.approaching_75_block,
            approaching_75_at = v.approaching_75_at,
            snipers_count = v.snipers_count,
            curve_native_reserve = v.curve_native_reserve,
            curve_token_reserve = v.curve_token_reserve,
            ath_price_native = GREATEST(t.ath_price_native, v.ath_price_native)
        FROM (VALUES %s) AS v(
            last_price_native, native_volume, token_volume, volume_usd, fees_usd,
            buy_count, sell_count, tx_count, circulating_supply, approaching_75,
            approaching_75_block, approaching_75_at, snipers_count,
            curve_native_reserve, curve_token_reserve, ath_price_native, token
        )
        WHERE t.token = v.token
        """,
        data,
        template="(%s::numeric, %s::numeric, %s::numeric, %s::numeric, %s::numeric, %s::bigint, %s::bigint, %s::bigint, %s::numeric, %s::boolean, %s::bigint, %s::bigint, %s::bigint, %s::numeric, %s::numeric, %s::numeric, %s::text)",
        page_size=1000,
    )


# write many user aggregate updates in one statement
def update_users_batch(user_updates: dict[str, dict], cur) -> None:
    if not user_updates:
        return
    data = [
        (addr, int(u["native_volume_delta"]), u["realized_delta"], u["trade_count_delta"])
        for addr, u in user_updates.items()
    ]
    execute_values(
        cur,
        """
        INSERT INTO launchpad_users (address, total_native_volume, total_realized_pnl_native, total_trades)
        VALUES %s
        ON CONFLICT (address) DO UPDATE SET
            total_native_volume = launchpad_users.total_native_volume + EXCLUDED.total_native_volume,
            total_realized_pnl_native = launchpad_users.total_realized_pnl_native + EXCLUDED.total_realized_pnl_native,
            total_trades = launchpad_users.total_trades + EXCLUDED.total_trades
        """,
        data,
        page_size=1000,
    )


# apply many position deltas in one statement
def upsert_positions_batch(position_updates: dict[tuple[str, str], dict], cur) -> None:
    if not position_updates:
        return
    data = []
    for (addr, tok), p in position_updates.items():
        balance_insert = max(int(p["balance_token_delta"]), 0)
        unrealized_insert = Decimal(balance_insert) * Decimal(p["last_price_native"])
        total_insert = Decimal(p["realized_pnl_delta"]) + unrealized_insert
        data.append(
            (
                addr,
                tok,
                int(p["token_bought_delta"]),
                int(p["token_sold_delta"]),
                int(p["native_spent_delta"]),
                int(p["native_received_delta"]),
                int(p["balance_token_delta"]),
                p["realized_pnl_delta"],
                unrealized_insert,
                total_insert,
                int(p["trade_count_delta"]),
                int(p["buy_count_delta"]),
                int(p["sell_count_delta"]),
                int(p.get("cost_basis_delta") or 0),
                p["last_price_native"],
            )
        )
    execute_values(
        cur,
        """
        INSERT INTO launchpad_positions (
            user_address, token, token_bought, token_sold, native_spent, native_received,
            balance_token, realized_pnl_native, unrealized_pnl_native, total_pnl_native,
            trade_count, buy_count, sell_count, cost_basis_native
        )
        VALUES %s
        ON CONFLICT (user_address, token) DO UPDATE SET
            token_bought = launchpad_positions.token_bought + EXCLUDED.token_bought,
            token_sold = launchpad_positions.token_sold + EXCLUDED.token_sold,
            native_spent = launchpad_positions.native_spent + EXCLUDED.native_spent,
            native_received = launchpad_positions.native_received + EXCLUDED.native_received,
            balance_token = GREATEST(launchpad_positions.balance_token + EXCLUDED.balance_token, 0),
            realized_pnl_native = launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native,
            trade_count = launchpad_positions.trade_count + EXCLUDED.trade_count,
            buy_count = launchpad_positions.buy_count + EXCLUDED.buy_count,
            sell_count = launchpad_positions.sell_count + EXCLUDED.sell_count,
            cost_basis_native = GREATEST(launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native, 0)
        """,
        [(d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9], d[10], d[11], d[12], d[13]) for d in data],
        page_size=1000,
    )
    for (addr, tok), p in position_updates.items():
        cur.execute(
            """
            UPDATE launchpad_positions SET
                unrealized_pnl_native = GREATEST(balance_token, 0) * %s,
                total_pnl_native = realized_pnl_native + GREATEST(balance_token, 0) * %s
            WHERE user_address = %s AND token = %s
            """,
            (p["last_price_native"], p["last_price_native"], addr, tok),
        )


# merge and write many ohlcv bar updates in one statement
def upsert_ohlcv_batch(ohlcv_data: list[tuple], cur) -> None:
    if not ohlcv_data:
        return
    aggregated: dict[tuple, dict] = {}
    for token, resolution_sec, bucket_start, price_native, native_amount in ohlcv_data:
        key = (token.lower(), int(resolution_sec), int(bucket_start))
        if key not in aggregated:
            aggregated[key] = {
                "open": price_native,
                "high": price_native,
                "low": price_native,
                "close": price_native,
                "volume": int(abs(native_amount)),
            }
        else:
            agg = aggregated[key]
            agg["high"] = max(agg["high"], price_native)
            agg["low"] = min(agg["low"], price_native)
            agg["close"] = price_native
            agg["volume"] += int(abs(native_amount))

    data = [(k[0], k[1], k[2], v["open"], v["high"], v["low"], v["close"], v["volume"]) for k, v in aggregated.items()]
    execute_values(
        cur,
        """
        INSERT INTO launchpad_ohlcv (
            token, resolution_sec, bucket_start, open_price, high_price, low_price, close_price, quote_volume
        )
        VALUES %s
        ON CONFLICT (token, resolution_sec, bucket_start) DO UPDATE SET
            high_price = GREATEST(launchpad_ohlcv.high_price, EXCLUDED.high_price),
            low_price = LEAST(launchpad_ohlcv.low_price, EXCLUDED.low_price),
            close_price = EXCLUDED.close_price,
            quote_volume = launchpad_ohlcv.quote_volume + EXCLUDED.quote_volume
        """,
        data,
        page_size=1000,
    )


# insert many snipers and return which rows were new
def add_snipers_batch(snipers: list[tuple[str, str]], cur) -> set[tuple[str, str]]:
    if not snipers:
        return set()
    data = [(t.lower(), u.lower()) for t, u in snipers]
    execute_values(
        cur,
        """
        INSERT INTO launchpad_snipers (token, user_address)
        VALUES %s
        ON CONFLICT (token, user_address) DO NOTHING
        """,
        data,
        page_size=1000,
    )
    return set(snipers)


# wipe derived pool and vault tables before a rebuild
def clear_derived_state_from_block(start_block: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as cur2:
            _clear_derived_state_impl(start_block, cur2)
    else:
        _clear_derived_state_impl(start_block, cur)


# the deletes behind clear derived state
def _clear_derived_state_impl(start_block: int, cur) -> None:
    cur.execute("DELETE FROM crystal_pool_tvl_samples")
    cur.execute("DELETE FROM crystal_pool_sync_events")
    cur.execute("DELETE FROM crystal_pool_lp_users")
    cur.execute("DELETE FROM crystal_pools")
    cur.execute("DELETE FROM crystal_vault_balance_samples")
    cur.execute("DELETE FROM crystal_vault_deposits")
    cur.execute("DELETE FROM crystal_vault_withdrawals")
    cur.execute("DELETE FROM crystal_vault_users")
    cur.execute("DELETE FROM crystal_vaults")
    cur.execute("DELETE FROM crystal_markets")
    cur.execute("DELETE FROM launchpad_trades")
    cur.execute("DELETE FROM launchpad_ohlcv")
    cur.execute("DELETE FROM launchpad_positions")
    cur.execute("DELETE FROM launchpad_snipers")
    cur.execute("DELETE FROM launchpad_users")
    cur.execute("DELETE FROM launchpad_tokens")
    cur.execute("DELETE FROM launchpad_pools")
    cur.execute("DELETE FROM launchpad_daily_pnl")
    cur.execute("DELETE FROM launchpad_blocks")


# lowest and highest block held in the raw log cache
def get_cached_block_range(cur=None) -> tuple[int | None, int | None]:
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute("SELECT MIN(number), MAX(number) FROM launchpad_block_logs")
            row = cur2.fetchone()
    else:
        cur.execute("SELECT MIN(number), MAX(number) FROM launchpad_block_logs")
        row = cur.fetchone()

    if not row or row[0] is None:
        return None, None
    return int(row[0]), int(row[1])


def tokens_changed_since(tokens: list[str], *, since_block: int = 0, since_ts: int = 0) -> set[str]:
    toks = [(t or "").lower() for t in tokens if t]
    if not toks or (not since_block and not since_ts):
        return set(toks)

    if since_block:
        trade_clause = "tr.block_number > %s"
        token_clause = "t.created_block > %s OR t.migrated_block > %s"
        args = (toks, since_block, toks, since_block, since_block)
    else:
        trade_clause = "tr.timestamp > %s"
        token_clause = "t.created_at > %s OR t.migrated_at > %s"
        args = (toks, since_ts, toks, since_ts, since_ts)

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT tr.token FROM launchpad_trades tr
             WHERE tr.token = ANY(%s) AND {trade_clause}
            UNION
            SELECT t.token FROM launchpad_tokens t
             WHERE t.token = ANY(%s) AND ({token_clause})
            """,
            args,
        )
        return {(r[0] or "").lower() for r in cur.fetchall()}


# remember where a token's metadata came from, so a failed fetch can be retried
# after a restart. the in memory queue does not survive one
def set_token_uri(token: str, token_uri: str, cur=None) -> None:
    tok = (token or "").lower()
    uri = (token_uri or "").strip()
    if not tok or not uri:
        return

    def _run(c):
        c.execute(
            "UPDATE launchpad_tokens SET token_uri = %s WHERE token = %s AND COALESCE(token_uri, '') = ''",
            (uri, tok),
        )

    if cur is None:
        with db_cursor() as c2:
            _run(c2)
    else:
        _run(cur)


# tokens that still have no image or description but do have somewhere to fetch it
# from. drives the sweep that makes metadata self healing across restarts
def tokens_missing_metadata(limit: int = 500) -> list[tuple[str, str]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, token_uri
            FROM launchpad_tokens
            WHERE COALESCE(metadata_cid, '') = ''
              AND COALESCE(token_uri, '') <> ''
            ORDER BY created_at DESC NULLS LAST
            LIMIT %s
            """,
            (int(limit),),
        )
        return [((r[0] or "").lower(), r[1] or "") for r in cur.fetchall()]


# tokens with no metadata and no uri to fetch it from. these predate the uri being
# persisted, so the sweep has to recover the uri from the token contract first
def tokens_missing_uri(limit: int = 200) -> list[str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token
            FROM launchpad_tokens
            WHERE COALESCE(metadata_cid, '') = ''
              AND COALESCE(token_uri, '') = ''
            ORDER BY created_at DESC NULLS LAST
            LIMIT %s
            """,
            (int(limit),),
        )
        return [(r[0] or "").lower() for r in cur.fetchall()]


# repair the stored generation for a token whose emitter proved the row wrong
def set_token_source(token: str, source: int, cur=None) -> None:
    sql = "UPDATE launchpad_tokens SET source = %s WHERE token = %s"
    params = (int(source), (token or "").lower())
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


# cached fee config for a dex pair, fetched from the pair's fee collector on chain.
# ok=false records a revert, meaning a plain pair with no collector, so the client
# can classify it as general without a chain read of its own
def upsert_pair_fees(
    pair: str,
    *,
    ok: bool,
    fee_collector: str = "",
    base_token: str = "",
    quote_token: str = "",
    creator_fee_rate: int = 0,
    curve_protocol_fee_rate: int = 0,
    dex_protocol_fee_rate: int = 0,
    fetched_at: int = 0,
) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_pair_fees
                (pair, ok, fee_collector, base_token, quote_token,
                 creator_fee_rate, curve_protocol_fee_rate, dex_protocol_fee_rate, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pair) DO UPDATE SET
                ok = EXCLUDED.ok,
                fee_collector = EXCLUDED.fee_collector,
                base_token = EXCLUDED.base_token,
                quote_token = EXCLUDED.quote_token,
                creator_fee_rate = EXCLUDED.creator_fee_rate,
                curve_protocol_fee_rate = EXCLUDED.curve_protocol_fee_rate,
                dex_protocol_fee_rate = EXCLUDED.dex_protocol_fee_rate,
                fetched_at = EXCLUDED.fetched_at;
            """,
            (
                (pair or "").lower(),
                bool(ok),
                (fee_collector or "").lower(),
                (base_token or "").lower(),
                (quote_token or "").lower(),
                int(creator_fee_rate),
                int(curve_protocol_fee_rate),
                int(dex_protocol_fee_rate),
                int(fetched_at),
            ),
        )


# cached row for a pair, none when never fetched
def get_pair_fees(pair: str) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT pair, ok, fee_collector, base_token, quote_token,
                   creator_fee_rate, curve_protocol_fee_rate, dex_protocol_fee_rate, fetched_at
            FROM launchpad_pair_fees WHERE pair = %s
            """,
            ((pair or "").lower(),),
        )
        r = cur.fetchone()
    if not r:
        return None
    return {
        "pair": r[0],
        "ok": bool(r[1]),
        "feeCollector": r[2] or None,
        "baseToken": r[3] or None,
        "quoteToken": r[4] or None,
        "creatorFeeRate": str(int(r[5] or 0)),
        "curveProtocolFeeRate": str(int(r[6] or 0)),
        "dexProtocolFeeRate": str(int(r[7] or 0)),
        "fetchedAt": int(r[8] or 0),
    }


# migrated nadfun pairs the sweep has not fetched fees for yet
def pairs_missing_fees(limit: int = 100) -> list[str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.market FROM launchpad_tokens t
            WHERE t.source <> 0 AND COALESCE(t.market, '') <> ''
              AND NOT EXISTS (SELECT 1 FROM launchpad_pair_fees f WHERE f.pair = LOWER(t.market))
            LIMIT %s
            """,
            (int(limit),),
        )
        return [(r[0] or "").lower() for r in cur.fetchall()]


# cached fee rows for many pairs in one round trip, keyed by pair
def get_pair_fees_batch(pairs: list[str]) -> dict[str, dict]:
    pairs = [(p or "").lower() for p in pairs if p]
    if not pairs:
        return {}
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT pair, ok, fee_collector, base_token, quote_token,
                   creator_fee_rate, curve_protocol_fee_rate, dex_protocol_fee_rate, fetched_at
            FROM launchpad_pair_fees WHERE pair = ANY(%s)
            """,
            (pairs,),
        )
        rows = cur.fetchall()
    out = {}
    for r in rows:
        out[r[0]] = {
            "pair": r[0],
            "ok": bool(r[1]),
            "feeCollector": r[2] or None,
            "baseToken": r[3] or None,
            "quoteToken": r[4] or None,
            "creatorFeeRate": str(int(r[5] or 0)),
            "curveProtocolFeeRate": str(int(r[6] or 0)),
            "dexProtocolFeeRate": str(int(r[7] or 0)),
            "fetchedAt": int(r[8] or 0),
        }
    return out


# taker fee per crystal market for many markets in one round trip
def get_taker_fees_batch(markets: list[str]) -> dict[str, str]:
    markets = [(m or "").lower() for m in markets if m]
    if not markets:
        return {}
    with db_cursor() as cur:
        cur.execute("SELECT LOWER(market), taker_fee FROM crystal_markets WHERE LOWER(market) = ANY(%s)", (markets,))
        return {r[0]: str(int(r[1] or 0)) for r in cur.fetchall()}
