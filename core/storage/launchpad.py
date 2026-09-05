from __future__ import annotations

import json
import re
import time
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json, execute_values

from .base import db_cursor

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"


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


def get_last_processed_block() -> str | None:
    with db_cursor() as cur:
        cur.execute("SELECT MAX(number) FROM launchpad_blocks;")
        row = cur.fetchone()

    if row is None:
        return None

    last = row[0]
    return int(last) if last is not None else None


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
    realized_native=0,
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
                    token_reserve,
                    realized_native
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    int(realized_native or 0),
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
                token_reserve,
                realized_native
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                int(realized_native or 0),
            ),
        )


def aggregate_token_from_trades(token: str, cur: psycopg2.extensions.cursor | None = None) -> dict:
    tok = (token or "").lower()

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


def update_user_on_trade(
    *,
    address: str,
    native_amount: int,
    realized_delta,
    trade_count_delta: int = 1,
    cur: psycopg2.extensions.cursor | None = None,
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
                VALUES (%s, %s, %s, %s)
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
                    int(trade_count_delta),
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
            VALUES (%s, %s, %s, %s)
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
                int(trade_count_delta),
            ),
        )


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
    unrealized_insert = Decimal(balance_insert) * Decimal(last_price_native) - Decimal(max(cb, 0))
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
                    unrealized_pnl_native = crystal_unrealized_pnl(
                        launchpad_positions.balance_token + EXCLUDED.balance_token,
                        launchpad_positions.token_bought + EXCLUDED.token_bought,
                        launchpad_positions.token_sold + EXCLUDED.token_sold,
                        launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native,
                        %s),
                    total_pnl_native = (
                        launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native
                    ) + crystal_unrealized_pnl(
                        launchpad_positions.balance_token + EXCLUDED.balance_token,
                        launchpad_positions.token_bought + EXCLUDED.token_bought,
                        launchpad_positions.token_sold + EXCLUDED.token_sold,
                        launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native,
                        %s);
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
                unrealized_pnl_native = crystal_unrealized_pnl(
                    launchpad_positions.balance_token + EXCLUDED.balance_token,
                    launchpad_positions.token_bought + EXCLUDED.token_bought,
                    launchpad_positions.token_sold + EXCLUDED.token_sold,
                    launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native,
                    %s),
                total_pnl_native = (
                    launchpad_positions.realized_pnl_native + EXCLUDED.realized_pnl_native
                ) + crystal_unrealized_pnl(
                    launchpad_positions.balance_token + EXCLUDED.balance_token,
                    launchpad_positions.token_bought + EXCLUDED.token_bought,
                    launchpad_positions.token_sold + EXCLUDED.token_sold,
                    launchpad_positions.cost_basis_native + EXCLUDED.cost_basis_native,
                    %s);
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


def upsert_ohlcv(
    *,
    token: str,
    resolution_sec: int,
    bucket_start: int,
    price_native,
    native_amount: int,
    mon_usd=0,
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
                    quote_volume,
                    mon_usd
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (token, resolution_sec, bucket_start) DO UPDATE
                SET
                    high_price = GREATEST(launchpad_ohlcv.high_price, EXCLUDED.high_price),
                    low_price = LEAST(launchpad_ohlcv.low_price, EXCLUDED.low_price),
                    close_price = EXCLUDED.close_price,
                    quote_volume = launchpad_ohlcv.quote_volume + EXCLUDED.quote_volume,
                    mon_usd = EXCLUDED.mon_usd;
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
                    mon_usd or 0,
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
                quote_volume,
                mon_usd
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (token, resolution_sec, bucket_start) DO UPDATE
            SET
                high_price = GREATEST(launchpad_ohlcv.high_price, EXCLUDED.high_price),
                low_price = LEAST(launchpad_ohlcv.low_price, EXCLUDED.low_price),
                close_price = EXCLUDED.close_price,
                quote_volume = launchpad_ohlcv.quote_volume + EXCLUDED.quote_volume,
                mon_usd = EXCLUDED.mon_usd;
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
                mon_usd or 0,
            ),
        )


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


def upsert_univ4_pool(
    *,
    pool_id: str,
    token_addr: str,
    native_addr: str,
    token_is_0: bool,
    learned_from: str = "swap",
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO univ4_pools (pool_id, token_addr, native_addr, token_is_0, learned_from)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (pool_id) DO NOTHING;
    """
    args = (pool_id.lower(), token_addr.lower(), native_addr.lower(), bool(token_is_0), learned_from)
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, args)
    else:
        cur.execute(sql, args)


def load_univ4_pools_for_state() -> list[tuple]:
    with db_cursor() as cur:
        cur.execute("SELECT pool_id, token_addr, native_addr, token_is_0 FROM univ4_pools")
        return cur.fetchall()


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


def load_all_pools():
    with db_cursor() as cur:
        cur.execute("""
            SELECT pool, token_addr, native_addr, token_is_0
            FROM launchpad_pools
        """)
        return cur.fetchall()


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


def _host_expr(col: str) -> str:
    return (
        "regexp_replace("
        f"split_part(split_part(lower(regexp_replace(COALESCE({col}, ''), '^https?://', '', 'i')), '/', 1), '?', 1)"
        r", '^www\.', '')"
    )


def _handle_expr(col: str) -> str:
    tw = r"'^(https?://)?(www\.)?(x|twitter)\.com/'"
    return (
        "CASE "
        f"WHEN COALESCE({col}, '') ~* {tw} THEN "
        f"lower(split_part(split_part(regexp_replace({col}, {tw}, '', 'i'), '/', 1), '?', 1)) "
        f"WHEN COALESCE({col}, '') ~ '^@' THEN lower(substring({col} from 2)) "
        "ELSE NULL END"
    )


def blacklist_clauses(ex: dict | None, alias: str = "t") -> tuple[list[str], list]:
    ex = ex or {}
    a = f"{alias}." if alias else ""
    where: list[str] = []
    params: list = []

    def norm(key):
        return [str(v).strip().lower() for v in (ex.get(key) or []) if str(v).strip()]

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
        clauses = [f"COALESCE({build(f'{a}social{i}')}, '')" for i in range(1, 5)]
        where.append("NOT (" + " OR ".join(f"({c}) = ANY(%s)" for c in clauses) + ")")
        params.extend([vals] * len(clauses))

    return where, params


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

    now = int(time.time())
    if f.get("age_min") is not None:
        where.append("t.created_at <= %s")
        params.append(now - int(float(f["age_min"]) * 60))
    if f.get("age_max") is not None:
        where.append("t.created_at >= %s")
        params.append(now - int(float(f["age_max"]) * 60))

    src = f.get("source")
    if src is not None and str(src).strip() != "":
        if int(src) == 0:
            where.append("t.source = 0")
        else:
            where.append("t.source <> 0")

    bl_where, bl_params = blacklist_clauses(f, "t")
    where.extend(bl_where)
    params.extend(bl_params)

    rng("t.volume_usd", "volume_24h_min", "volume_24h_max")
    rng("t.fees_usd", "fees_min", "fees_max")
    rng("t.buy_count", "buy_tx_min", "buy_tx_max")
    rng("t.sell_count", "sell_tx_min", "sell_tx_max")
    rng("t.last_price_native", "price_min", "price_max")

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


def load_holder_denylist() -> list[str]:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT address FROM holder_denylist")
            return [r[0].lower() for r in cur.fetchall() if r[0]]
    except Exception:
        return []


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


def load_nadfun_v2_tokens() -> list[str]:
    try:
        with db_cursor() as cur:
            cur.execute("SELECT token FROM nadfun_v2_tokens")
            return [r[0].lower() for r in cur.fetchall() if r[0]]
    except Exception:
        return []


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


def write_block_logs_batch(blocks: dict[int, list[dict]], cur) -> None:
    if not blocks:
        return
    data = [(blk, Json(logs or [])) for blk, logs in blocks.items()]
    execute_values(
        cur,
        "INSERT INTO launchpad_block_logs (number, logs) VALUES %s ON CONFLICT (number) DO NOTHING",
        data,
    )


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


def list_blocks_with_addresses(start_block: int, end_block: int, addresses: list[str], cur=None) -> list[int]:
    clauses = " OR ".join(["logs @> %s::jsonb"] * len(addresses))
    params = [start_block, end_block] + [json.dumps([{"address": a.lower()}]) for a in addresses]
    sql = f"""
        SELECT number FROM launchpad_block_logs
        WHERE number BETWEEN %s AND %s AND ({clauses})
        ORDER BY number
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
            return [int(r[0]) for r in cur2.fetchall()]
    cur.execute(sql, params)
    return [int(r[0]) for r in cur.fetchall()]


def get_block_logs_for(numbers: list[int], cur=None) -> dict[int, list[dict]]:
    if not numbers:
        return {}
    sql = "SELECT number, logs FROM launchpad_block_logs WHERE number = ANY(%s)"
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, (list(numbers),))
            return {int(n): (lg or []) for n, lg in cur2.fetchall()}
    cur.execute(sql, (list(numbers),))
    return {int(n): (lg or []) for n, lg in cur.fetchall()}


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


def insert_trades_batch(trades: list[tuple], cur) -> None:
    if not trades:
        return
    execute_values(
        cur,
        """
        INSERT INTO launchpad_trades (
            block_number, log_index, timestamp, token, user_address,
            is_buy, native_amount, token_amount, usd_amount, price_native, txhash,
            native_reserve, token_reserve, realized_native
        )
        VALUES %s
        ON CONFLICT (txhash, log_index) DO NOTHING
        """,
        trades,
        page_size=1000,
    )


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


def upsert_positions_batch(position_updates: dict[tuple[str, str], dict], cur) -> None:
    if not position_updates:
        return
    data = []
    for (addr, tok), p in position_updates.items():
        balance_insert = max(int(p["balance_token_delta"]), 0)
        unrealized_insert = Decimal(balance_insert) * Decimal(p["last_price_native"]) - Decimal(
            max(int(p["cost_basis_delta"]), 0)
        )
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
                unrealized_pnl_native = crystal_unrealized_pnl(
                    balance_token, token_bought, token_sold, cost_basis_native, %s),
                total_pnl_native = realized_pnl_native + crystal_unrealized_pnl(
                    balance_token, token_bought, token_sold, cost_basis_native, %s)
            WHERE user_address = %s AND token = %s
            """,
            (p["last_price_native"], p["last_price_native"], addr, tok),
        )


def upsert_ohlcv_batch(ohlcv_data: list[tuple], cur) -> None:
    if not ohlcv_data:
        return
    aggregated: dict[tuple, dict] = {}
    for token, resolution_sec, bucket_start, price_native, native_amount, mon_usd in ohlcv_data:
        key = (token.lower(), int(resolution_sec), int(bucket_start))
        if key not in aggregated:
            aggregated[key] = {
                "open": price_native,
                "high": price_native,
                "low": price_native,
                "close": price_native,
                "volume": int(abs(native_amount)),
                "mon_usd": mon_usd or 0,
            }
        else:
            agg = aggregated[key]
            agg["high"] = max(agg["high"], price_native)
            agg["low"] = min(agg["low"], price_native)
            agg["close"] = price_native
            agg["volume"] += int(abs(native_amount))
            agg["mon_usd"] = mon_usd or 0

    data = [
        (k[0], k[1], k[2], v["open"], v["high"], v["low"], v["close"], v["volume"], v["mon_usd"])
        for k, v in aggregated.items()
    ]
    execute_values(
        cur,
        """
        INSERT INTO launchpad_ohlcv (
            token, resolution_sec, bucket_start, open_price, high_price, low_price, close_price, quote_volume,
            mon_usd
        )
        VALUES %s
        ON CONFLICT (token, resolution_sec, bucket_start) DO UPDATE SET
            high_price = GREATEST(launchpad_ohlcv.high_price, EXCLUDED.high_price),
            low_price = LEAST(launchpad_ohlcv.low_price, EXCLUDED.low_price),
            close_price = EXCLUDED.close_price,
            quote_volume = launchpad_ohlcv.quote_volume + EXCLUDED.quote_volume,
            mon_usd = EXCLUDED.mon_usd
        """,
        data,
        page_size=1000,
    )


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


CRYSTAL_LAUNCHPAD_SOURCE = 0


def crystal_generation_counts(before_block: int, cur) -> dict[str, int]:
    cur.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM launchpad_tokens WHERE source = %(src)s AND created_block < %(blk)s),
            (SELECT COUNT(*) FROM crystal_markets WHERE created_block < %(blk)s)
        """,
        {"src": CRYSTAL_LAUNCHPAD_SOURCE, "blk": int(before_block)},
    )
    tokens, markets = cur.fetchone()
    return {"tokens": int(tokens or 0), "markets": int(markets or 0)}


def delete_crystal_generation_before(before_block: int, cur) -> dict[str, int]:
    params = {"src": CRYSTAL_LAUNCHPAD_SOURCE, "blk": int(before_block)}
    old_tokens = "(SELECT token FROM launchpad_tokens WHERE source = %(src)s AND created_block < %(blk)s)"
    old_markets = "(SELECT market FROM crystal_markets WHERE created_block < %(blk)s)"

    steps = [
        ("launchpad_trades", f"DELETE FROM launchpad_trades WHERE token IN {old_tokens}"),
        ("launchpad_positions", f"DELETE FROM launchpad_positions WHERE token IN {old_tokens}"),
        ("launchpad_ohlcv", f"DELETE FROM launchpad_ohlcv WHERE token IN {old_tokens}"),
        ("launchpad_snipers", f"DELETE FROM launchpad_snipers WHERE token IN {old_tokens}"),
        ("crystal_orderbook_orders", f"DELETE FROM crystal_orderbook_orders WHERE market IN {old_markets}"),
        ("crystal_orderbook_fills", f"DELETE FROM crystal_orderbook_fills WHERE market IN {old_markets}"),
        ("crystal_orderbook_events", f"DELETE FROM crystal_orderbook_events WHERE market IN {old_markets}"),
        ("crystal_market_trades", f"DELETE FROM crystal_market_trades WHERE market IN {old_markets}"),
        ("crystal_pool_liquidity_events", f"DELETE FROM crystal_pool_liquidity_events WHERE market IN {old_markets}"),
        ("crystal_pool_tvl_samples", f"DELETE FROM crystal_pool_tvl_samples WHERE market IN {old_markets}"),
        ("crystal_pool_sync_events", f"DELETE FROM crystal_pool_sync_events WHERE market IN {old_markets}"),
        ("crystal_pool_lp_users", f"DELETE FROM crystal_pool_lp_users WHERE market IN {old_markets}"),
        ("crystal_pools", f"DELETE FROM crystal_pools WHERE market IN {old_markets}"),
        ("launchpad_tokens", f"DELETE FROM launchpad_tokens WHERE token IN {old_tokens}"),
        ("crystal_markets", "DELETE FROM crystal_markets WHERE created_block < %(blk)s"),
    ]

    removed: dict[str, int] = {}
    for name, sql in steps:
        cur.execute(sql, params)
        removed[name] = max(cur.rowcount, 0)
    return removed


def nadfun_row_counts(cur) -> dict[str, int]:
    cur.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM launchpad_tokens WHERE source <> %(src)s),
            (SELECT COUNT(*) FROM launchpad_trades t JOIN launchpad_tokens k ON k.token = t.token
                WHERE k.source <> %(src)s),
            (SELECT COUNT(*) FROM launchpad_positions p JOIN launchpad_tokens k ON k.token = p.token
                WHERE k.source <> %(src)s)
        """,
        {"src": CRYSTAL_LAUNCHPAD_SOURCE},
    )
    tokens, trades, positions = cur.fetchone()
    return {"tokens": int(tokens or 0), "trades": int(trades or 0), "positions": int(positions or 0)}


def clear_derived_state_from_block(start_block: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as cur2:
            _clear_derived_state_impl(start_block, cur2)
    else:
        _clear_derived_state_impl(start_block, cur)


def _clear_derived_state_impl(start_block: int, cur) -> None:
    cur.execute("DELETE FROM crystal_pool_tvl_samples")
    cur.execute("DELETE FROM crystal_pool_sync_events")
    cur.execute("DELETE FROM crystal_pool_lp_users")
    cur.execute("DELETE FROM crystal_pools")
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
    cur.execute("DELETE FROM crystal_orderbook_events")
    cur.execute("DELETE FROM crystal_market_trades")
    cur.execute("DELETE FROM crystal_orderbook_orders")
    cur.execute("DELETE FROM crystal_orderbook_fills")
    cur.execute("DELETE FROM launchpad_blocks")


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


def tokens_missing_metadata(limit: int = 500) -> list[tuple[str, str]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, token_uri
            FROM launchpad_tokens
            WHERE COALESCE(metadata_cid, '') = ''
              AND COALESCE(token_uri, '') <> ''
              AND metadata_tried_at
                  + LEAST(3600 * POWER(2, LEAST(metadata_attempts, 8)), 604800) <= %s
            ORDER BY metadata_tried_at ASC, random()
            LIMIT %s
            """,
            (int(time.time()), int(limit)),
        )
        return [((r[0] or "").lower(), r[1] or "") for r in cur.fetchall()]


def mark_metadata_attempted(tokens: list[str], cur=None) -> None:
    if not tokens:
        return

    def _run(c) -> None:
        c.execute(
            """
            UPDATE launchpad_tokens
            SET metadata_attempts = metadata_attempts + 1,
                metadata_tried_at = %s
            WHERE token = ANY(%s)
            """,
            (int(time.time()), [t.lower() for t in tokens]),
        )

    if cur is None:
        with db_cursor() as c2:
            _run(c2)
    else:
        _run(cur)


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


def set_token_source(token: str, source: int, cur=None) -> None:
    sql = "UPDATE launchpad_tokens SET source = %s WHERE token = %s"
    params = (int(source), (token or "").lower())
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


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
    pool_fee_ppm: int = 0,
    fetched_at: int = 0,
) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_pair_fees
                (pair, ok, fee_collector, base_token, quote_token,
                 creator_fee_rate, curve_protocol_fee_rate, dex_protocol_fee_rate,
                 pool_fee_ppm, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pair) DO UPDATE SET
                ok = EXCLUDED.ok,
                fee_collector = EXCLUDED.fee_collector,
                base_token = EXCLUDED.base_token,
                quote_token = EXCLUDED.quote_token,
                creator_fee_rate = EXCLUDED.creator_fee_rate,
                curve_protocol_fee_rate = EXCLUDED.curve_protocol_fee_rate,
                dex_protocol_fee_rate = EXCLUDED.dex_protocol_fee_rate,
                pool_fee_ppm = EXCLUDED.pool_fee_ppm,
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
                int(pool_fee_ppm),
                int(fetched_at),
            ),
        )


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


def pairs_missing_fees(limit: int = 100) -> list[str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.market FROM launchpad_tokens t
            WHERE t.source <> 0 AND COALESCE(t.market, '') <> ''
              AND NOT EXISTS (
                    SELECT 1 FROM launchpad_pair_fees f
                    WHERE f.pair = LOWER(t.market) AND (f.ok OR f.pool_fee_ppm > 0)
              )
            LIMIT %s
            """,
            (int(limit),),
        )
        return [(r[0] or "").lower() for r in cur.fetchall()]


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


def get_taker_fees_batch(markets: list[str]) -> dict[str, str]:
    markets = [(m or "").lower() for m in markets if m]
    if not markets:
        return {}
    with db_cursor() as cur:
        cur.execute("SELECT LOWER(market), taker_fee FROM crystal_markets WHERE LOWER(market) = ANY(%s)", (markets,))
        return {r[0]: str(int(r[1] or 0)) for r in cur.fetchall()}


def get_pool_fee_rate(market: str, source: int, cur=None) -> Decimal | None:
    market = (market or "").lower()
    if not market:
        return None
    if int(source or 0) == 0:
        sql = "SELECT taker_fee, 0, TRUE FROM crystal_markets WHERE LOWER(market) = %s"
    else:
        sql = """
            SELECT creator_fee_rate + dex_protocol_fee_rate, pool_fee_ppm, ok
            FROM launchpad_pair_fees WHERE pair = %s
        """
    if cur is not None:
        cur.execute(sql, (market,))
        row = cur.fetchone()
    else:
        with db_cursor() as c:
            c.execute(sql, (market,))
            row = c.fetchone()
    if not row:
        return None
    if int(source or 0) == 0:
        if row[0] is None:
            return None
        rate = (Decimal(100000) - Decimal(row[0])) / Decimal(100000)
    elif row[2] and row[0]:
        rate = Decimal(row[0]) / Decimal(10000)
    elif row[1]:
        rate = Decimal(row[1]) / Decimal(1000000)
    else:
        return None
    if rate <= 0 or rate >= Decimal("0.05"):
        return None
    return rate


def set_meta(key: str, value: str, cur=None) -> None:
    sql = """
        INSERT INTO launchpad_kv (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """
    if cur is not None:
        cur.execute(sql, (key, str(value)))
        return
    with db_cursor() as c:
        c.execute(sql, (key, str(value)))


def get_meta(key: str) -> str | None:
    with db_cursor() as cur:
        cur.execute("SELECT value FROM launchpad_kv WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def record_chain_tip(number: int, block_hash: str, cur=None) -> None:
    set_meta("tip_block", str(int(number)), cur=cur)
    set_meta("tip_hash", (block_hash or "").lower(), cur=cur)


def record_dex_tip(number: int, block_timestamp: int, cur=None) -> None:
    set_meta("dex_tip_block", str(int(number)), cur=cur)
    set_meta("dex_tip_ts", str(int(block_timestamp)), cur=cur)


def wallet_has_crystal_activity(wallet: str) -> bool:
    addr = (wallet or "").lower()
    if not addr:
        return False
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (SELECT 1 FROM launchpad_positions WHERE user_address = %(a)s)
                OR EXISTS (SELECT 1 FROM crystal_orderbook_events WHERE user_address = %(a)s)
                OR EXISTS (SELECT 1 FROM crystal_pool_lp_users WHERE user_address = %(a)s)
                OR EXISTS (SELECT 1 FROM crystal_vault_users WHERE user_address = %(a)s)
            """,
            {"a": addr},
        )
        row = cur.fetchone()
    return bool(row and row[0])


def write_spot_graph_bucket(
    wallet: str,
    bucket_ts: int,
    block_number: int,
    value_usd,
    value_native,
    balances: dict,
    cur=None,
) -> None:
    sql = """
        INSERT INTO spot_graph_buckets (wallet, bucket_ts, block_number, value_usd, value_native, balances)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (wallet, bucket_ts) DO UPDATE SET
            block_number = EXCLUDED.block_number,
            value_usd = EXCLUDED.value_usd,
            value_native = EXCLUDED.value_native,
            balances = EXCLUDED.balances
    """
    params = ((wallet or "").lower(), int(bucket_ts), int(block_number), value_usd, value_native, Json(balances))
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


def get_spot_graph_buckets(
    wallet: str, since_ts: int, value_version: int | None = None
) -> list[tuple[int, Decimal, Decimal]]:
    version_filter = ""
    params: list = [(wallet or "").lower(), int(since_ts)]
    if value_version is not None:
        version_filter = "AND COALESCE(balances ->> '__valueVersion', '0') = %s"
        params.append(str(int(value_version)))
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT bucket_ts, value_usd, value_native
            FROM spot_graph_buckets
            WHERE wallet = %s AND bucket_ts >= %s
              {version_filter}
            ORDER BY bucket_ts
            """,
            tuple(params),
        )
        return [(int(t), v or Decimal(0), n or Decimal(0)) for t, v, n in cur.fetchall()]


def get_spot_graph_bucket_set(wallet: str, since_ts: int, value_version: int | None = None) -> set[int]:
    version_filter = ""
    params: list = [(wallet or "").lower(), int(since_ts)]
    if value_version is not None:
        version_filter = "AND COALESCE(balances ->> '__valueVersion', '0') = %s"
        params.append(str(int(value_version)))
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT bucket_ts FROM spot_graph_buckets
            WHERE wallet = %s AND bucket_ts >= %s {version_filter}
            """,
            tuple(params),
        )
        return {int(r[0]) for r in cur.fetchall()}


def count_uncached_processed_blocks(start_block: int, end_block: int, cur=None) -> int:
    sql = """
        SELECT COUNT(*)
        FROM launchpad_blocks b
        LEFT JOIN launchpad_block_logs l ON l.number = b.number
        WHERE b.number BETWEEN %s AND %s AND l.number IS NULL
    """
    if cur is not None:
        cur.execute(sql, (start_block, end_block))
        return int(cur.fetchone()[0])
    with db_cursor() as c:
        c.execute(sql, (start_block, end_block))
        return int(c.fetchone()[0])


def latest_trade_timestamp() -> int:
    with db_cursor() as cur:
        cur.execute("SELECT MAX(timestamp) FROM launchpad_trades")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def update_pool_reserves(pool: str, reserve0: int, reserve1: int, blk: int, blk_ts: int, cur=None) -> None:
    sql = """
        UPDATE launchpad_pools
        SET reserve_token = CASE WHEN token_is_0 THEN %s ELSE %s END,
            reserve_native = CASE WHEN token_is_0 THEN %s ELSE %s END,
            last_sync_block = %s,
            last_sync_at = %s
        WHERE pool = %s AND COALESCE(last_sync_block, 0) <= %s
    """
    args = (
        int(reserve0),
        int(reserve1),
        int(reserve1),
        int(reserve0),
        int(blk),
        int(blk_ts),
        (pool or "").lower(),
        int(blk),
    )
    if cur is None:
        with db_cursor() as c:
            c.execute(sql, args)
    else:
        cur.execute(sql, args)


def pool_reserves_for_tokens(tokens: list[str], cur=None) -> dict[str, dict]:
    if not tokens:
        return {}
    sql = """
        SELECT token_addr, pool, reserve_token, reserve_native, last_sync_at
        FROM launchpad_pools
        WHERE token_addr = ANY(%s)
    """
    args = ([t.lower() for t in tokens],)
    if cur is None:
        with db_cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
    else:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return {
        t: {"pool": p, "reserveToken": str(int(rt)), "reserveNative": str(int(rn)), "syncedAt": int(ts or 0)}
        for t, p, rt, rn, ts in rows
    }


def mon_usd_series(start_ts: int, end_ts: int, resolution: int, min_wei: int) -> list[tuple[int, float]]:
    resolution = max(int(resolution), 1)
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT g AS bucket, lt.rate
            FROM generate_series(%s / %s * %s, %s, %s) g
            LEFT JOIN LATERAL (
                SELECT usd_amount / (native_amount / 1e18) AS rate
                FROM launchpad_trades
                WHERE timestamp >= g AND timestamp < g + %s
                  AND native_amount >= %s
                  AND usd_amount > 0
                ORDER BY timestamp DESC, block_number DESC, log_index DESC
                LIMIT 1
            ) lt ON true
            WHERE lt.rate IS NOT NULL
            ORDER BY g
            """,
            (int(start_ts), resolution, resolution, int(end_ts), resolution, resolution, int(min_wei)),
        )
        return [(int(b), float(r)) for b, r in cur.fetchall() if r is not None]


def wallet_activity(
    users: list[str],
    limit: int = 50,
    before_ts: int | None = None,
    before_key: tuple[int, int, int, str] | None = None,
) -> list[dict]:
    addrs = [str(u).lower() for u in (users or [])]
    if not addrs:
        return []
    params: dict = {"u": addrs, "lim": int(limit)}

    def _cut(alias: str) -> str:
        if before_key is not None:
            return (
                f" AND ({alias}.timestamp, {alias}.block_number, {alias}.log_index, {alias}.txhash)"
                " < (%(cts)s, %(cblk)s, %(cli)s, %(ctx)s)"
            )
        if before_ts is not None:
            return f" AND {alias}.timestamp < %(cut)s"
        return ""

    if before_key is not None:
        params["cts"], params["cblk"], params["cli"], params["ctx"] = (
            int(before_key[0]),
            int(before_key[1]),
            int(before_key[2]),
            str(before_key[3]),
        )
    elif before_ts is not None:
        params["cut"] = int(before_ts)

    cut_t, cut_d, cut_w, cut_c, cut_e, cut_b, cut_o, cut_f, cut_m, cut_r, cut_k = (
        _cut("t"),
        _cut("d"),
        _cut("w"),
        _cut("c"),
        _cut("e"),
        _cut("b"),
        _cut("o"),
        _cut("f"),
        _cut("mt"),
        _cut("r"),
        _cut("k"),
    )

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT CASE WHEN t.is_buy THEN 'buy' ELSE 'sell' END AS kind,
                       t.timestamp, t.block_number, t.txhash, t.log_index,
                       t.token AS subject, k.symbol, k.name,
                       t.native_amount AS amount_native, t.token_amount AS amount_token,
                       t.price_native, t.usd_amount
                FROM launchpad_trades t
                JOIN launchpad_tokens k ON k.token = t.token
                WHERE t.user_address = ANY(%(u)s){cut_t}
                UNION ALL
                SELECT 'vault_deposit', d.timestamp, d.block_number, d.txhash, d.log_index,
                       d.vault, v.name, v.name, d.quote_amount, d.base_amount, 0, 0
                FROM crystal_vault_deposits d
                LEFT JOIN crystal_vaults v ON v.vault = d.vault
                WHERE d.user_address = ANY(%(u)s){cut_d}
                UNION ALL
                SELECT 'vault_withdraw', w.timestamp, w.block_number, w.txhash, w.log_index,
                       w.vault, v.name, v.name, w.quote_amount, w.base_amount, 0, 0
                FROM crystal_vault_withdrawals w
                LEFT JOIN crystal_vaults v ON v.vault = w.vault
                WHERE w.user_address = ANY(%(u)s){cut_w}
                UNION ALL
                SELECT 'fee_claim', c.timestamp, c.block_number, c.txhash, c.log_index,
                       c.token,
                       CASE WHEN c.token = '0x3bd359c1119da7da1d913d1c4d2b7c461115433a' THEN 'WMON' ELSE COALESCE(k2.symbol, '') END,
                       CASE WHEN c.token = '0x3bd359c1119da7da1d913d1c4d2b7c461115433a' THEN 'Wrapped MON' ELSE COALESCE(k2.name, '') END,
                       CASE WHEN c.token = '0x3bd359c1119da7da1d913d1c4d2b7c461115433a' THEN c.amount ELSE 0 END,
                       CASE WHEN c.token = '0x3bd359c1119da7da1d913d1c4d2b7c461115433a' THEN 0 ELSE c.amount END,
                       0, 0
                FROM referral_claims c
                LEFT JOIN launchpad_tokens k2 ON k2.token = c.token
                WHERE c.user_address = ANY(%(u)s){cut_c}
                UNION ALL
                SELECT CASE WHEN e.kind = 'mint' THEN 'lp_deposit' ELSE 'lp_withdraw' END,
                       e.timestamp, e.block_number, e.txhash, e.log_index,
                       e.market, COALESCE(m.base_ticker, ''), COALESCE(m.base_name, ''),
                       e.amount_quote, e.amount_base, 0, 0
                FROM crystal_pool_liquidity_events e
                LEFT JOIN crystal_markets m ON m.market = e.market
                WHERE e.user_address = ANY(%(u)s){cut_e}
                UNION ALL
                SELECT CASE WHEN b.kind = 'deposit' THEN 'balance_deposit' ELSE 'balance_withdraw' END,
                       b.timestamp, b.block_number, b.txhash, b.log_index,
                       b.token, COALESCE(k3.symbol, ''), COALESCE(k3.name, ''),
                       b.amount, 0, 0, 0
                FROM crystal_balance_events b
                LEFT JOIN launchpad_tokens k3 ON k3.token = b.token
                WHERE b.user_address = ANY(%(u)s){cut_b}
                UNION ALL
                SELECT CASE o.action
                           WHEN 'add' THEN 'order_place'
                           WHEN 'remove' THEN 'order_cancel'
                           ELSE 'order_update'
                       END,
                       o.timestamp, o.block_number, o.txhash, o.log_index,
                       o.market, COALESCE(m2.base_ticker, ''), COALESCE(m2.base_name, ''),
                       0, o.size, o.price, 0
                FROM crystal_orderbook_events o
                LEFT JOIN crystal_markets m2 ON m2.market = o.market
                WHERE o.user_address = ANY(%(u)s){cut_o}
                UNION ALL
                SELECT 'order_fill', f.timestamp, f.block_number, f.txhash, f.log_index,
                       f.market, COALESCE(m3.base_ticker, ''), COALESCE(m3.base_name, ''),
                       f.amount_high, f.amount_out, f.price, 0
                FROM crystal_orderbook_fills f
                LEFT JOIN crystal_markets m3 ON m3.market = f.market
                WHERE f.maker = ANY(%(u)s){cut_f}
                UNION ALL
                SELECT CASE WHEN mt.is_buy THEN 'taker_buy' ELSE 'taker_sell' END,
                       mt.timestamp, mt.block_number, mt.txhash, mt.log_index,
                       mt.market, COALESCE(m4.base_ticker, ''), COALESCE(m4.base_name, ''),
                       CASE WHEN mt.is_buy THEN mt.amount_in ELSE mt.amount_out END,
                       CASE WHEN mt.is_buy THEN mt.amount_out ELSE mt.amount_in END,
                       mt.end_price, 0
                FROM crystal_market_trades mt
                LEFT JOIN crystal_markets m4 ON m4.market = mt.market
                WHERE mt.user_address = ANY(%(u)s){cut_m}
                  -- a graduated launchpad token trades on a crystal market too, and
                  -- the same log lands in both tables. the launchpad row carries the
                  -- symbol and price, so drop the duplicate here
                  AND NOT EXISTS (
                      SELECT 1 FROM launchpad_trades lt
                      WHERE lt.txhash = mt.txhash AND lt.log_index = mt.log_index
                        AND lt.user_address = mt.user_address
                  )
                UNION ALL
                SELECT 'referral_use', r.timestamp, r.block_number,
                       CONCAT('referral-', r.referee, '-', r.block_number, '-', r.log_index), r.log_index,
                       CASE WHEN r.referee = ANY(%(u)s) THEN r.referrer ELSE r.referee END,
                       '', '', 0, 0, 0, 0
                FROM referral_bindings r
                WHERE (r.referee = ANY(%(u)s) OR r.referrer = ANY(%(u)s)){cut_r}
                UNION ALL
                SELECT 'token_create', k.created_at, k.created_block,
                       CONCAT('token-create-', k.token), 0,
                       k.token, k.symbol, k.name, 0, 0, k.last_price_native, 0
                FROM launchpad_tokens k
                WHERE k.creator = ANY(%(u)s){cut_k}
                UNION ALL
                SELECT 'token_graduate', k.migrated_at, k.migrated_block,
                       CONCAT('token-graduate-', k.token), 1,
                       k.token, k.symbol, k.name, 0, 0, k.last_price_native, 0
                FROM launchpad_tokens k
                WHERE k.creator = ANY(%(u)s) AND k.migrated = TRUE AND k.migrated_at > 0{cut_k}
            ) a
            ORDER BY timestamp DESC, block_number DESC, log_index DESC, txhash DESC
            LIMIT %(lim)s
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "type": kind,
            "timestamp": int(ts or 0),
            "blockNumber": int(blk or 0),
            "txhash": txh,
            "logIndex": int(li or 0),
            "subject": subject,
            "symbol": symbol or "",
            "name": name or "",
            "amountNative": str(int(amt_native or 0)),
            "amountToken": str(int(amt_token or 0)),
            "priceNative": str(price or 0),
            "usdAmount": str(usd or 0),
        }
        for kind, ts, blk, txh, li, subject, symbol, name, amt_native, amt_token, price, usd in rows
    ]


def set_lvmon_rate(value) -> None:
    val = Decimal(value)
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_meta (key, value)
            VALUES ('lvmon_mon_rate', %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value;
            """,
            (val,),
        )


def get_lvmon_rate():
    with db_cursor() as cur:
        cur.execute("SELECT value FROM launchpad_meta WHERE key = 'lvmon_mon_rate';")
        row = cur.fetchone()
    return row[0] if row else None


def record_revenue_sample(block_number: int, timestamp: int, balance_wei: int, mon_price_usd, cur=None) -> dict | None:
    def _run(c):
        c.execute("SELECT block_number, balance_wei FROM crystal_revenue_samples ORDER BY block_number DESC LIMIT 1")
        prev = c.fetchone()
        if prev is not None and int(prev[0]) >= int(block_number):
            return None
        delta = Decimal(balance_wei) - (Decimal(prev[1]) if prev else Decimal(balance_wei))
        if delta < 0:
            delta = Decimal(0)
        price = Decimal(str(mon_price_usd or 0))
        delta_usd = (delta / Decimal(10) ** 18) * price
        c.execute(
            """
            INSERT INTO crystal_revenue_samples
                (block_number, timestamp, balance_wei, delta_wei, mon_price_usd, delta_usd)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (block_number) DO NOTHING
            """,
            (int(block_number), int(timestamp), int(balance_wei), int(delta), price, delta_usd),
        )
        return {"delta_wei": int(delta), "delta_usd": delta_usd, "first": prev is None}

    if cur is not None:
        return _run(cur)
    with db_cursor() as c:
        return _run(c)


def revenue_totals(now_ts: int) -> dict:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(delta_wei), 0),
                COALESCE(SUM(delta_usd), 0),
                COALESCE(SUM(delta_wei) FILTER (WHERE timestamp > %(d1)s), 0),
                COALESCE(SUM(delta_usd) FILTER (WHERE timestamp > %(d1)s), 0),
                COALESCE(SUM(delta_wei) FILTER (WHERE timestamp > %(d7)s), 0),
                COALESCE(SUM(delta_usd) FILTER (WHERE timestamp > %(d7)s), 0),
                COUNT(*),
                MAX(timestamp),
                MAX(block_number)
            FROM crystal_revenue_samples
            """,
            {"d1": int(now_ts) - 86400, "d7": int(now_ts) - 7 * 86400},
        )
        r = cur.fetchone()
        cur.execute("SELECT balance_wei FROM crystal_revenue_samples ORDER BY block_number DESC LIMIT 1")
        bal = cur.fetchone()
    return {
        "tracked_native": int(r[0] or 0),
        "tracked_usd": Decimal(r[1] or 0),
        "native_24h": int(r[2] or 0),
        "usd_24h": Decimal(r[3] or 0),
        "native_7d": int(r[4] or 0),
        "usd_7d": Decimal(r[5] or 0),
        "samples": int(r[6] or 0),
        "last_timestamp": int(r[7] or 0),
        "last_block": int(r[8] or 0),
        "balance_native": int(bal[0]) if bal else 0,
    }


def list_revenue_samples(start_ts: int, limit: int = 500) -> list[tuple]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT block_number, timestamp, balance_wei, delta_wei, delta_usd
            FROM crystal_revenue_samples
            WHERE timestamp >= %s
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            (int(start_ts), int(max(1, min(limit, 5000)))),
        )
        return cur.fetchall()


def insert_crystal_balance_event(
    *,
    txhash: str,
    log_index: int,
    kind: str,
    user_address: str,
    user_id: int,
    token: str,
    amount: int,
    block_number: int,
    timestamp: int,
    cur=None,
) -> None:
    sql = """
        INSERT INTO crystal_balance_events
            (txhash, log_index, kind, user_address, user_id, token, amount, block_number, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING
    """
    params = (
        (txhash or "").lower(),
        int(log_index),
        kind,
        (user_address or "").lower(),
        int(user_id or 0),
        (token or "").lower(),
        int(amount or 0),
        int(block_number),
        int(timestamp),
    )
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


def insert_crystal_protocol_event(
    *, txhash: str, log_index: int, kind: str, params: dict, block_number: int, timestamp: int, cur=None
) -> None:
    sql = """
        INSERT INTO crystal_protocol_events (txhash, log_index, kind, params, block_number, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING
    """
    args = ((txhash or "").lower(), int(log_index), kind, Json(params or {}), int(block_number), int(timestamp))
    if cur is not None:
        cur.execute(sql, args)
        return
    with db_cursor() as c:
        c.execute(sql, args)


def list_crystal_protocol_events(kind: str = "", limit: int = 50) -> list[dict]:
    where = "WHERE kind = %s" if kind else ""
    args: tuple = (kind, int(limit)) if kind else (int(limit),)
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT kind, params, block_number, timestamp, txhash
            FROM crystal_protocol_events {where}
            ORDER BY block_number DESC, log_index DESC
            LIMIT %s
            """,
            args,
        )
        return [
            {"kind": k, "params": p, "blockNumber": int(b or 0), "timestamp": int(t or 0), "txhash": tx}
            for k, p, b, t, tx in cur.fetchall()
        ]


def graduated_holdings(wallets: list[str]) -> list[dict]:
    addrs = [str(w or "").lower() for w in (wallets or []) if w]
    if not addrs:
        return []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.token, MAX(t.symbol), MAX(t.name), SUM(p.balance_token), MAX(t.last_price_native)
            FROM launchpad_positions p
            JOIN launchpad_tokens t ON t.token = p.token
            WHERE p.user_address = ANY(%s) AND p.balance_token > 0 AND t.migrated
            GROUP BY p.token
            """,
            (addrs,),
        )
        rows = cur.fetchall()
    return [
        {
            "token": tok,
            "symbol": sym or "",
            "name": name or "",
            "balance_raw": int(bal or 0),
            "last_price_native": lp or 0,
        }
        for tok, sym, name, bal, lp in rows
    ]


def pnl_24h(wallets: list[str], cutoff_ts: int) -> tuple[Decimal, Decimal]:
    addrs = [str(w or "").lower() for w in (wallets or []) if w]
    if not addrs:
        return Decimal(0), Decimal(0)
    with db_cursor() as cur:
        cur.execute(
            """
            WITH realized AS (
                SELECT COALESCE(SUM(realized_native), 0) AS v
                FROM launchpad_trades
                WHERE user_address = ANY(%(a)s) AND timestamp >= %(cut)s
            ), unrealized AS (
                SELECT COALESCE(SUM(
                    p.balance_token * (t.last_price_native - COALESCE(o.close_price, t.last_price_native))
                ), 0) AS v
                FROM launchpad_positions p
                JOIN launchpad_tokens t ON t.token = p.token
                LEFT JOIN LATERAL (
                    SELECT close_price FROM launchpad_ohlcv
                    WHERE token = p.token AND resolution_sec = 3600 AND bucket_start <= %(cut)s
                    ORDER BY bucket_start DESC
                    LIMIT 1
                ) o ON TRUE
                WHERE p.user_address = ANY(%(a)s) AND p.balance_token > 0
            )
            SELECT realized.v, unrealized.v FROM realized, unrealized
            """,
            {"a": addrs, "cut": int(cutoff_ts)},
        )
        row = cur.fetchone()
    if not row:
        return Decimal(0), Decimal(0)
    return Decimal(row[0] or 0), Decimal(row[1] or 0)


def pools_needing_reserve_refresh(limit: int = 200, stale_seconds: int = 300, lookback_seconds: int = 86400):
    now = int(time.time())
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.pool, p.token_addr, p.token_is_0, COALESCE(p.last_sync_at, 0) AS synced_at
            FROM launchpad_pools p
            JOIN LATERAL (
                SELECT MAX(timestamp) AS last_trade_at
                FROM launchpad_trades
                WHERE token = p.token_addr AND timestamp > %s
            ) t ON TRUE
            WHERE t.last_trade_at IS NOT NULL
              AND COALESCE(p.last_sync_at, 0) < %s
            ORDER BY t.last_trade_at DESC
            LIMIT %s
            """,
            (now - int(lookback_seconds), now - int(stale_seconds), int(limit)),
        )
        return cur.fetchall()


def force_pool_reserves(pool: str, reserve_token: int, reserve_native: int, blk: int, blk_ts: int, cur=None) -> None:
    sql = """
        UPDATE launchpad_pools
        SET reserve_token = %s,
            reserve_native = %s,
            last_sync_block = GREATEST(COALESCE(last_sync_block, 0), %s),
            last_sync_at = %s
        WHERE pool = %s
    """
    args = (int(reserve_token), int(reserve_native), int(blk), int(blk_ts), (pool or "").lower())
    if cur is None:
        with db_cursor() as c:
            c.execute(sql, args)
    else:
        cur.execute(sql, args)


def token_images_by_address() -> dict[str, str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, metadata_cid FROM launchpad_tokens
            WHERE COALESCE(metadata_cid, '') <> ''
            """
        )
        return {t: c for t, c in cur.fetchall() if t and c}


def last_trade_ts_by_token(wallets: list[str]) -> dict[str, int]:
    addrs = [str(w or "").lower() for w in (wallets or []) if w]
    if not addrs:
        return {}
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, MAX(timestamp) FROM launchpad_trades
            WHERE user_address = ANY(%s)
            GROUP BY token
            """,
            (addrs,),
        )
        return {t: int(ts) for t, ts in cur.fetchall() if t and ts}
