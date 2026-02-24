from __future__ import annotations
from typing import Optional, Iterator
from contextlib import contextmanager
from decimal import Decimal

import os
import threading
import psycopg2

from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import Json, execute_values

_DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL", "postgresql://postgres:ShIsCu2024;1@localhost:5432/logs")
_DB_MIN_CONN: int = 5
_DB_MAX_CONN: int = 125

_POOL: Optional[ThreadedConnectionPool] = None
_POOL_LOCK = threading.Lock()


def _clean_text(value) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.replace("\x00", "")


def init_pool() -> None:
    global _POOL
    
    if _DATABASE_URL is None:
        raise RuntimeError("[DB] Missing DB URL")
    
    with _POOL_LOCK:
        if _POOL is not None:
            return
        
        _POOL = ThreadedConnectionPool(
            minconn=_DB_MIN_CONN,
            maxconn=_DB_MAX_CONN,
            dsn=_DATABASE_URL,
        )


def close_pool() -> None:
    global _POOL
    
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.closeall()
        _POOL = None


def _get_pool() -> ThreadedConnectionPool:
    global _POOL
    
    if _POOL is None:
        raise RuntimeError("[DB] Uninitialized connection pool")

    return _POOL



@contextmanager
def db_cursor() -> Iterator[psycopg2.extensions.cursor]:
    pool = _get_pool()
    conn = pool.getconn()
    
    try:
        if conn.autocommit:
            conn.autocommit = False
        
        cur = conn.cursor()
        
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        pool.putconn(conn)
    

def init_db() -> None:
    with db_cursor() as cur:

        cur.execute(
            """
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_blocks
            (
                number       BIGINT PRIMARY KEY,
                processed_at TIMESTAMPTZ NOT NULL DEFAULT Now()
            ); 
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_block_logs
            (
                number BIGINT PRIMARY KEY,
                logs   JSONB NOT NULL
            )
            """
        )
        

        cur.execute(
           """
            CREATE TABLE IF NOT EXISTS launchpad_trades
            (
                id            BIGSERIAL PRIMARY KEY,
                block_number  BIGINT NOT NULL,
                log_index     INTEGER NOT NULL,
                timestamp     BIGINT NOT NULL,
                token         TEXT NOT NULL,
                user_address  TEXT NOT NULL,
                is_buy        BOOLEAN NOT NULL,
                native_amount NUMERIC(50, 0) NOT NULL,
                token_amount  NUMERIC(50, 0) NOT NULL,
                usd_amount    NUMERIC(50, 18) NOT NULL,
                price_native  NUMERIC(50, 18) NOT NULL,
                txhash        TEXT NOT NULL,
                UNIQUE (txhash, log_index)
            ); 
           """ 
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_token_ts 
            ON launchpad_trades (token, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_ts 
            ON launchpad_trades (user_address, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_block
            ON launchpad_trades (block_number);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_token
            ON launchpad_trades (user_address, token);
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_tokens
            (
                token                TEXT PRIMARY KEY,
                creator              TEXT NOT NULL,
                name                 TEXT NOT NULL,
                symbol               TEXT NOT NULL,
                metadata_cid         TEXT,
                description          TEXT,
                social1              TEXT,
                social2              TEXT,
                social3              TEXT,
                social4              TEXT,
                source               INTEGER NOT NULL,
                created_block        BIGINT NOT NULL,
                created_at           BIGINT NOT NULL,
                migrated             BOOLEAN NOT NULL DEFAULT false,
                migrated_block       BIGINT,
                migrated_at          BIGINT,
                market               TEXT,
                last_price_native    NUMERIC(50, 18) NOT NULL DEFAULT 0,
                native_volume        NUMERIC(50, 0) NOT NULL DEFAULT 0,
                token_volume         NUMERIC(50, 0) NOT NULL DEFAULT 0,
                volume_usd           NUMERIC(50, 18) NOT NULL DEFAULT 0,
                fees_usd             NUMERIC(50, 18) NOT NULL DEFAULT 0,
                buy_count            BIGINT NOT NULL DEFAULT 0,
                sell_count           BIGINT NOT NULL DEFAULT 0,
                tx_count             BIGINT NOT NULL DEFAULT 0,
                circulating_supply   NUMERIC(50, 0) NOT NULL DEFAULT 0,
                snipers_count        BIGINT NOT NULL DEFAULT 0,
                approaching_75       BOOLEAN NOT NULL DEFAULT false,
                approaching_75_block BIGINT,
                approaching_75_at    BIGINT
            ); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_creator 
            ON launchpad_tokens (creator); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_created_at 
            ON launchpad_tokens (created_at DESC); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_migrated_at 
            ON launchpad_tokens (migrated, migrated_at DESC); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_name_trgm
            ON launchpad_tokens
            USING gin (name gin_trgm_ops);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_symbol_trgm
            ON launchpad_tokens
            USING gin (symbol gin_trgm_ops);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_token_trgm
            ON launchpad_tokens
            USING gin (token gin_trgm_ops);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_approaching
            ON launchpad_tokens (circulating_supply DESC)
            WHERE approaching_75 = TRUE AND migrated = FALSE;
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_users
            (
                address                   TEXT PRIMARY KEY,
                tokens_created            INTEGER NOT NULL DEFAULT 0,
                tokens_graduated          INTEGER NOT NULL DEFAULT 0,
                total_native_volume       NUMERIC(50, 0) NOT NULL DEFAULT 0,
                total_realized_pnl_native NUMERIC(50, 18) NOT NULL DEFAULT 0,
                total_trades              BIGINT NOT NULL DEFAULT 0
            ); 
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_positions
            (
                user_address          TEXT NOT NULL,
                token                 TEXT NOT NULL,
                token_bought          NUMERIC(50, 0) NOT NULL DEFAULT 0,
                token_sold            NUMERIC(50, 0) NOT NULL DEFAULT 0,
                native_spent          NUMERIC(50, 0) NOT NULL DEFAULT 0,
                native_received       NUMERIC(50, 0) NOT NULL DEFAULT 0,
                balance_token         NUMERIC(50, 0) NOT NULL DEFAULT 0,
                realized_pnl_native   NUMERIC(50, 18) NOT NULL DEFAULT 0,
                unrealized_pnl_native NUMERIC(50, 18) NOT NULL DEFAULT 0,
                total_pnl_native      NUMERIC(50, 18) NOT NULL DEFAULT 0,
                trade_count           BIGINT NOT NULL DEFAULT 0,
                buy_count             BIGINT NOT NULL DEFAULT 0,
                sell_count            BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (user_address, token)
            ); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_user 
            ON launchpad_positions (user_address); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_token_balance
            ON launchpad_positions (token, balance_token DESC)
            WHERE balance_token > 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_token_total_pnl
            ON launchpad_positions (token, total_pnl_native DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_history_keyset
            ON launchpad_trades (user_address, timestamp DESC, log_index DESC, txhash DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_token_history_keyset
            ON launchpad_trades (user_address, token, timestamp DESC, log_index DESC, txhash DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_user_pnl_keyset
            ON launchpad_positions (user_address, total_pnl_native DESC, token DESC)
            WHERE balance_token > 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_user_balance_keyset
            ON launchpad_positions (user_address, balance_token DESC, token DESC)
            WHERE balance_token > 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_users_pnl_leaderboard
            ON launchpad_users (total_realized_pnl_native DESC)
            WHERE total_realized_pnl_native > 0;
            """
        )


        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_pools
            (
                pool        TEXT PRIMARY KEY,
                token_addr  TEXT NOT NULL,
                native_addr TEXT NOT NULL,
                token_is_0  BOOLEAN NOT NULL
            ); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pools_token 
            ON launchpad_pools (token_addr); 
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_ohlcv
            (
                token          TEXT NOT NULL,
                resolution_sec INTEGER NOT NULL,
                bucket_start   BIGINT NOT NULL,
                open_price     NUMERIC(50, 18) NOT NULL,
                high_price     NUMERIC(50, 18) NOT NULL,
                low_price      NUMERIC(50, 18) NOT NULL,
                close_price    NUMERIC(50, 18) NOT NULL,
                quote_volume   NUMERIC(50, 0) NOT NULL,
                PRIMARY KEY (token, resolution_sec, bucket_start)
            ); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ohlcv_token_res_time 
            ON launchpad_ohlcv (token, resolution_sec, bucket_start DESC); 
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_daily_pnl
            (
                user_address          TEXT NOT NULL,
                day                   DATE NOT NULL,
                realized_pnl_native   NUMERIC(50, 18) NOT NULL DEFAULT 0,
                unrealized_pnl_native NUMERIC(50, 18) NOT NULL DEFAULT 0,
                fees_native           NUMERIC(50, 18) NOT NULL DEFAULT 0,
                volume_native         NUMERIC(50, 0) NOT NULL DEFAULT 0,
                trade_count           BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (user_address, day)
            ); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_pnl_user_day 
            ON launchpad_daily_pnl (user_address, day); 
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_snipers
            (
                token        TEXT NOT NULL,
                user_address TEXT NOT NULL,
                PRIMARY KEY (token, user_address)
            ); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snipers_token 
            ON launchpad_snipers (token); 
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snipers_user 
            ON launchpad_snipers (user_address); 
            """
        )
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_meta
            (
                key   TEXT PRIMARY KEY,
                value NUMERIC(50, 18) NOT NULL
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_markets
            (
                market         TEXT PRIMARY KEY,
                is_canonical   BOOLEAN NOT NULL,
                quote_asset    TEXT NOT NULL,
                base_asset     TEXT NOT NULL,
                quote_address  TEXT NOT NULL,
                quote_decimals INTEGER NOT NULL,
                quote_ticker   TEXT NOT NULL,
                quote_name     TEXT NOT NULL,
                base_address   TEXT NOT NULL,
                base_decimals  INTEGER NOT NULL,
                base_ticker    TEXT NOT NULL,
                base_name      TEXT NOT NULL,
                market_id      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                market_type    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                scale_factor   NUMERIC(78, 0) NOT NULL DEFAULT 0,
                tick_size      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                max_price      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                min_size       NUMERIC(78, 0) NOT NULL DEFAULT 0,
                taker_fee      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                maker_rebate   NUMERIC(78, 0) NOT NULL DEFAULT 0,
                last_price     NUMERIC(50, 18) NOT NULL DEFAULT 0,
                created_block  BIGINT,
                created_at     BIGINT,
                updated_block  BIGINT,
                updated_at     BIGINT
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_markets_quote_base
            ON crystal_markets (quote_address, base_address);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vaults
            (
                vault                 TEXT PRIMARY KEY,
                quote                 TEXT NOT NULL,
                base                  TEXT NOT NULL,
                market                TEXT NOT NULL DEFAULT '',
                owner                 TEXT NOT NULL,
                name                  TEXT NOT NULL DEFAULT '',
                description           TEXT NOT NULL DEFAULT '',
                social1               TEXT NOT NULL DEFAULT '',
                social2               TEXT NOT NULL DEFAULT '',
                social3               TEXT NOT NULL DEFAULT '',
                locked                BOOLEAN NOT NULL DEFAULT FALSE,
                closed                BOOLEAN NOT NULL DEFAULT FALSE,
                max_shares            NUMERIC(78, 0) NOT NULL DEFAULT 0,
                circulating_shares    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                quote_decimals        INTEGER NOT NULL DEFAULT 0,
                base_decimals         INTEGER NOT NULL DEFAULT 0,
                lockup                NUMERIC(78, 0) NOT NULL DEFAULT 0,
                decrease_on_withdraw  BOOLEAN NOT NULL DEFAULT FALSE,
                deployed_block        BIGINT,
                deployed_at           BIGINT,
                updated_block         BIGINT,
                updated_at            BIGINT
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vaults_quote_base
            ON crystal_vaults (quote, base);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_users
            (
                vault         TEXT NOT NULL,
                user_address  TEXT NOT NULL,
                shares        NUMERIC(78, 0) NOT NULL DEFAULT 0,
                deposits      BIGINT NOT NULL DEFAULT 0,
                withdraws     BIGINT NOT NULL DEFAULT 0,
                last_deposit  BIGINT NOT NULL DEFAULT 0,
                last_withdraw BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (vault, user_address)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_vault_shares
            ON crystal_vault_users (vault, shares DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_vault_lastdep
            ON crystal_vault_users (vault, last_deposit DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_vault_lastdep_shares_addr
            ON crystal_vault_users (vault, last_deposit DESC, shares DESC, user_address ASC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_deposits
            (
                id           BIGSERIAL PRIMARY KEY,
                block_number BIGINT NOT NULL,
                log_index    INTEGER NOT NULL,
                timestamp    BIGINT NOT NULL,
                vault        TEXT NOT NULL,
                user_address TEXT NOT NULL,
                shares       NUMERIC(78, 0) NOT NULL,
                quote_amount NUMERIC(78, 0) NOT NULL,
                base_amount  NUMERIC(78, 0) NOT NULL,
                txhash       TEXT NOT NULL,
                UNIQUE (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_deposits_vault_ts
            ON crystal_vault_deposits (vault, timestamp DESC, log_index DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_withdrawals
            (
                id           BIGSERIAL PRIMARY KEY,
                block_number BIGINT NOT NULL,
                log_index    INTEGER NOT NULL,
                timestamp    BIGINT NOT NULL,
                vault        TEXT NOT NULL,
                user_address TEXT NOT NULL,
                shares       NUMERIC(78, 0) NOT NULL,
                quote_amount NUMERIC(78, 0) NOT NULL,
                base_amount  NUMERIC(78, 0) NOT NULL,
                txhash       TEXT NOT NULL,
                UNIQUE (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_withdrawals_vault_ts
            ON crystal_vault_withdrawals (vault, timestamp DESC, log_index DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_balance_samples
            (
                vault         TEXT NOT NULL,
                block_number  BIGINT NOT NULL,
                timestamp     BIGINT NOT NULL,
                quote_balance NUMERIC(78, 0) NOT NULL,
                base_balance  NUMERIC(78, 0) NOT NULL,
                usd_value     NUMERIC(50, 18) NOT NULL,
                PRIMARY KEY (vault, block_number)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_balances_vault_ts
            ON crystal_vault_balance_samples (vault, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_balances_vault_ts_block
            ON crystal_vault_balance_samples (vault, timestamp DESC, block_number DESC);
            """
        )


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
        
def get_last_processed_block() -> Optional[str]:
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
    cur: psycopg2.extensions.cursor | None = None
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
                    txhash
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                txhash
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            ),
        )

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
    cur: psycopg2.extensions.cursor | None = None
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
                    snipers_count = %s
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
                snipers_count = %s
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
                token.lower(),
            ),
        )

def update_user_on_trade(
    *,
    address: str,
    native_amount: int,
    realized_delta,
    cur: psycopg2.extensions.cursor | None = None
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
    cur: psycopg2.extensions.cursor | None = None
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
                    sell_count
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                sell_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
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
                    last_price_native
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE
                SET
                    creator = EXCLUDED.creator,
                    name = EXCLUDED.name,
                    symbol = EXCLUDED.symbol,
                    metadata_cid = EXCLUDED.metadata_cid,
                    description = EXCLUDED.description,
                    social1 = EXCLUDED.social1,
                    social2 = EXCLUDED.social2,
                    social3 = EXCLUDED.social3,
                    social4 = EXCLUDED.social4,
                    source = EXCLUDED.source,
                    created_block = EXCLUDED.created_block,
                    created_at = EXCLUDED.created_at,
                    last_price_native = EXCLUDED.last_price_native;
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
                last_price_native
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (token) DO UPDATE
            SET
                creator = EXCLUDED.creator,
                name = EXCLUDED.name,
                symbol = EXCLUDED.symbol,
                metadata_cid = EXCLUDED.metadata_cid,
                description = EXCLUDED.description,
                social1 = EXCLUDED.social1,
                social2 = EXCLUDED.social2,
                social3 = EXCLUDED.social3,
                social4 = EXCLUDED.social4,
                source = EXCLUDED.source,
                created_block = EXCLUDED.created_block,
                created_at = EXCLUDED.created_at,
                last_price_native = EXCLUDED.last_price_native;
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
    pool: Optional[str],
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
                    market = %s
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
                market = %s
            WHERE token = %s;
            """,
            (int(migrated_block), int(migrated_at), pool_addr, tok),
        )

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

def increment_user_tokens_graduated(address: str,     cur: psycopg2.extensions.cursor | None = None) -> None:
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
                approaching_75_at
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
                q, q, q,
                prefix, prefix, prefix,
                contains, contains, contains,
                q, q, q,
                contains, contains, contains,
                q, q, q,
                limit,
            ),
        )
        return cur.fetchall()
    
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
    cur.executemany(
        """
        INSERT INTO launchpad_block_logs (number, logs)
        VALUES (%s, %s)
        ON CONFLICT (number) DO NOTHING
        """,
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
            is_buy, native_amount, token_amount, usd_amount, price_native, txhash
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
        data.append((
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
            token.lower(),
        ))
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
            snipers_count = v.snipers_count
        FROM (VALUES %s) AS v(
            last_price_native, native_volume, token_volume, volume_usd, fees_usd,
            buy_count, sell_count, tx_count, circulating_supply, approaching_75,
            approaching_75_block, approaching_75_at, snipers_count, token
        )
        WHERE t.token = v.token
        """,
        data,
        template="(%s::numeric, %s::numeric, %s::numeric, %s::numeric, %s::numeric, %s::bigint, %s::bigint, %s::bigint, %s::numeric, %s::boolean, %s::bigint, %s::bigint, %s::bigint, %s::text)",
        page_size=1000,
    )


def update_users_batch(user_updates: dict[str, dict], cur) -> None:
    if not user_updates:
        return
    data = [(addr, int(u["native_volume_delta"]), u["realized_delta"], u["trade_count_delta"]) for addr, u in user_updates.items()]
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
        unrealized_insert = Decimal(balance_insert) * Decimal(p["last_price_native"])
        total_insert = Decimal(p["realized_pnl_delta"]) + unrealized_insert
        data.append((
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
            p["last_price_native"],
        ))
    execute_values(
        cur,
        """
        INSERT INTO launchpad_positions (
            user_address, token, token_bought, token_sold, native_spent, native_received,
            balance_token, realized_pnl_native, unrealized_pnl_native, total_pnl_native,
            trade_count, buy_count, sell_count
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
            sell_count = launchpad_positions.sell_count + EXCLUDED.sell_count
        """,
        [(d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9], d[10], d[11], d[12]) for d in data],
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

    data = [
        (k[0], k[1], k[2], v["open"], v["high"], v["low"], v["close"], v["volume"])
        for k, v in aggregated.items()
    ]
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


def clear_derived_state_from_block(start_block: int, cur=None) -> None:
    if cur is None:
        with db_cursor() as cur2:
            _clear_derived_state_impl(start_block, cur2)
    else:
        _clear_derived_state_impl(start_block, cur)


def _clear_derived_state_impl(start_block: int, cur) -> None:
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


def upsert_crystal_market(
    *,
    market: str,
    is_canonical: bool,
    quote_asset: str,
    base_asset: str,
    quote_address: str,
    quote_decimals: int,
    quote_ticker: str,
    quote_name: str,
    base_address: str,
    base_decimals: int,
    base_ticker: str,
    base_name: str,
    market_id: int,
    market_type: int,
    scale_factor: int,
    tick_size: int,
    max_price: int,
    min_size: int,
    taker_fee: int,
    maker_rebate: int,
    created_block: int | None,
    created_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        market.lower(),
        bool(is_canonical),
        quote_asset.lower(),
        base_asset.lower(),
        quote_address.lower(),
        int(quote_decimals),
        _clean_text(quote_ticker),
        _clean_text(quote_name),
        base_address.lower(),
        int(base_decimals),
        _clean_text(base_ticker),
        _clean_text(base_name),
        int(market_id or 0),
        int(market_type or 0),
        int(scale_factor or 0),
        int(tick_size or 0),
        int(max_price or 0),
        int(min_size or 0),
        int(taker_fee or 0),
        int(maker_rebate or 0),
        int(created_block) if created_block is not None else None,
        int(created_at) if created_at is not None else None,
        int(created_block) if created_block is not None else None,
        int(created_at) if created_at is not None else None,
    )
    sql = """
        INSERT INTO crystal_markets (
            market, is_canonical, quote_asset, base_asset, quote_address, quote_decimals,
            quote_ticker, quote_name, base_address, base_decimals, base_ticker, base_name,
            market_id, market_type, scale_factor, tick_size, max_price, min_size, taker_fee, maker_rebate,
            created_block, created_at, updated_block, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market) DO UPDATE SET
            is_canonical = EXCLUDED.is_canonical,
            quote_asset = EXCLUDED.quote_asset,
            base_asset = EXCLUDED.base_asset,
            quote_address = EXCLUDED.quote_address,
            quote_decimals = EXCLUDED.quote_decimals,
            quote_ticker = EXCLUDED.quote_ticker,
            quote_name = EXCLUDED.quote_name,
            base_address = EXCLUDED.base_address,
            base_decimals = EXCLUDED.base_decimals,
            base_ticker = EXCLUDED.base_ticker,
            base_name = EXCLUDED.base_name,
            market_id = EXCLUDED.market_id,
            market_type = EXCLUDED.market_type,
            scale_factor = EXCLUDED.scale_factor,
            tick_size = EXCLUDED.tick_size,
            max_price = EXCLUDED.max_price,
            min_size = EXCLUDED.min_size,
            taker_fee = EXCLUDED.taker_fee,
            maker_rebate = EXCLUDED.maker_rebate,
            updated_block = COALESCE(EXCLUDED.updated_block, crystal_markets.updated_block),
            updated_at = COALESCE(EXCLUDED.updated_at, crystal_markets.updated_at);
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def update_crystal_market_price(
    market: str,
    last_price,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        Decimal(last_price),
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        market.lower(),
    )
    sql = """
        UPDATE crystal_markets
        SET last_price = %s,
            updated_block = COALESCE(%s, updated_block),
            updated_at = COALESCE(%s, updated_at)
        WHERE market = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def link_crystal_vaults_for_market(
    *,
    quote: str,
    base: str,
    market: str,
    quote_decimals: int,
    base_decimals: int,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        market.lower(),
        int(quote_decimals or 0),
        int(base_decimals or 0),
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        quote.lower(),
        base.lower(),
    )
    sql = """
        UPDATE crystal_vaults
        SET market = CASE WHEN market = '' THEN %s ELSE market END,
            quote_decimals = CASE WHEN quote_decimals = 0 THEN %s ELSE quote_decimals END,
            base_decimals = CASE WHEN base_decimals = 0 THEN %s ELSE base_decimals END,
            updated_block = COALESCE(%s, updated_block),
            updated_at = COALESCE(%s, updated_at)
        WHERE quote = %s AND base = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def load_crystal_markets_for_state():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market, is_canonical, quote_asset, base_asset, quote_address, quote_decimals,
                quote_ticker, quote_name, base_address, base_decimals, base_ticker, base_name,
                market_id, market_type, scale_factor, tick_size, max_price, min_size, taker_fee, maker_rebate,
                last_price
            FROM crystal_markets
            """
        )
        return cur.fetchall()


def list_crystal_pool_markets():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market,
                quote_address,
                base_address,
                market_type,
                quote_decimals,
                base_decimals,
                quote_ticker,
                quote_name,
                base_ticker,
                base_name,
                taker_fee,
                last_price,
                updated_at,
                created_at
            FROM crystal_markets
            WHERE is_canonical = TRUE
              AND market_type NOT IN (0, 1)
            ORDER BY COALESCE(updated_at, created_at, 0) DESC, market ASC
            """
        )
        return cur.fetchall()


def get_crystal_pool_market(market: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market,
                quote_address,
                base_address,
                market_type,
                quote_decimals,
                base_decimals,
                quote_ticker,
                quote_name,
                base_ticker,
                base_name,
                taker_fee,
                last_price,
                updated_at,
                created_at
            FROM crystal_markets
            WHERE is_canonical = TRUE
              AND market_type NOT IN (0, 1)
              AND market = %s
            LIMIT 1
            """,
            ((market or "").lower(),),
        )
        return cur.fetchone()


def upsert_crystal_vault(
    *,
    vault: str,
    quote: str,
    base: str,
    market: str,
    owner: str,
    name: str,
    description: str,
    social1: str,
    social2: str,
    social3: str,
    locked: bool,
    closed: bool,
    max_shares: int,
    circulating_shares: int,
    quote_decimals: int,
    base_decimals: int,
    lockup: int,
    decrease_on_withdraw: bool,
    deployed_block: int | None,
    deployed_at: int | None,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        vault.lower(),
        quote.lower(),
        base.lower(),
        (market or "").lower(),
        owner.lower(),
        _clean_text(name),
        _clean_text(description),
        _clean_text(social1),
        _clean_text(social2),
        _clean_text(social3),
        bool(locked),
        bool(closed),
        int(max_shares or 0),
        int(circulating_shares or 0),
        int(quote_decimals or 0),
        int(base_decimals or 0),
        int(lockup or 0),
        bool(decrease_on_withdraw),
        int(deployed_block) if deployed_block is not None else None,
        int(deployed_at) if deployed_at is not None else None,
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
    )
    sql = """
        INSERT INTO crystal_vaults (
            vault, quote, base, market, owner, name, description, social1, social2, social3,
            locked, closed, max_shares, circulating_shares, quote_decimals, base_decimals,
            lockup, decrease_on_withdraw, deployed_block, deployed_at, updated_block, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (vault) DO UPDATE SET
            quote = EXCLUDED.quote,
            base = EXCLUDED.base,
            market = CASE WHEN EXCLUDED.market <> '' THEN EXCLUDED.market ELSE crystal_vaults.market END,
            owner = EXCLUDED.owner,
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            social1 = EXCLUDED.social1,
            social2 = EXCLUDED.social2,
            social3 = EXCLUDED.social3,
            locked = EXCLUDED.locked,
            closed = EXCLUDED.closed,
            max_shares = EXCLUDED.max_shares,
            circulating_shares = EXCLUDED.circulating_shares,
            quote_decimals = CASE WHEN EXCLUDED.quote_decimals <> 0 THEN EXCLUDED.quote_decimals ELSE crystal_vaults.quote_decimals END,
            base_decimals = CASE WHEN EXCLUDED.base_decimals <> 0 THEN EXCLUDED.base_decimals ELSE crystal_vaults.base_decimals END,
            lockup = EXCLUDED.lockup,
            decrease_on_withdraw = EXCLUDED.decrease_on_withdraw,
            deployed_block = COALESCE(crystal_vaults.deployed_block, EXCLUDED.deployed_block),
            deployed_at = COALESCE(crystal_vaults.deployed_at, EXCLUDED.deployed_at),
            updated_block = COALESCE(EXCLUDED.updated_block, crystal_vaults.updated_block),
            updated_at = COALESCE(EXCLUDED.updated_at, crystal_vaults.updated_at);
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def update_crystal_vault_fields(
    *,
    vault: str,
    locked: bool | None = None,
    closed: bool | None = None,
    max_shares: int | None = None,
    circulating_shares: int | None = None,
    lockup: int | None = None,
    decrease_on_withdraw: bool | None = None,
    market: str | None = None,
    quote_decimals: int | None = None,
    base_decimals: int | None = None,
    updated_block: int | None = None,
    updated_at: int | None = None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        UPDATE crystal_vaults
        SET locked = COALESCE(%s, locked),
            closed = COALESCE(%s, closed),
            max_shares = COALESCE(%s, max_shares),
            circulating_shares = COALESCE(%s, circulating_shares),
            lockup = COALESCE(%s, lockup),
            decrease_on_withdraw = COALESCE(%s, decrease_on_withdraw),
            market = COALESCE(NULLIF(%s, ''), market),
            quote_decimals = COALESCE(%s, quote_decimals),
            base_decimals = COALESCE(%s, base_decimals),
            updated_block = COALESCE(%s, updated_block),
            updated_at = COALESCE(%s, updated_at)
        WHERE vault = %s;
    """
    params = (
        locked,
        closed,
        int(max_shares) if max_shares is not None else None,
        int(circulating_shares) if circulating_shares is not None else None,
        int(lockup) if lockup is not None else None,
        decrease_on_withdraw,
        (market or "") if market is not None else None,
        int(quote_decimals) if quote_decimals is not None else None,
        int(base_decimals) if base_decimals is not None else None,
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        vault.lower(),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def insert_crystal_vault_deposit(
    *,
    block_number: int,
    log_index: int,
    timestamp: int,
    vault: str,
    user_address: str,
    shares: int,
    quote_amount: int,
    base_amount: int,
    txhash: str,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_vault_deposits (
            block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING;
    """
    params = (
        int(block_number),
        int(log_index),
        int(timestamp),
        vault.lower(),
        user_address.lower(),
        int(shares or 0),
        int(quote_amount or 0),
        int(base_amount or 0),
        txhash.lower(),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def insert_crystal_vault_withdrawal(
    *,
    block_number: int,
    log_index: int,
    timestamp: int,
    vault: str,
    user_address: str,
    shares: int,
    quote_amount: int,
    base_amount: int,
    txhash: str,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_vault_withdrawals (
            block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING;
    """
    params = (
        int(block_number),
        int(log_index),
        int(timestamp),
        vault.lower(),
        user_address.lower(),
        int(shares or 0),
        int(quote_amount or 0),
        int(base_amount or 0),
        txhash.lower(),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def upsert_crystal_vault_user_delta(
    *,
    vault: str,
    user_address: str,
    shares_delta: int,
    deposits_delta: int,
    withdraws_delta: int,
    last_deposit: int | None = None,
    last_withdraw: int | None = None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_vault_users (
            vault, user_address, shares, deposits, withdraws, last_deposit, last_withdraw
        )
        VALUES (%s, %s, GREATEST(%s, 0), %s, %s, COALESCE(%s, 0), COALESCE(%s, 0))
        ON CONFLICT (vault, user_address) DO UPDATE SET
            shares = GREATEST(crystal_vault_users.shares + %s, 0),
            deposits = crystal_vault_users.deposits + EXCLUDED.deposits,
            withdraws = crystal_vault_users.withdraws + EXCLUDED.withdraws,
            last_deposit = CASE
                WHEN EXCLUDED.last_deposit > crystal_vault_users.last_deposit THEN EXCLUDED.last_deposit
                ELSE crystal_vault_users.last_deposit
            END,
            last_withdraw = CASE
                WHEN EXCLUDED.last_withdraw > crystal_vault_users.last_withdraw THEN EXCLUDED.last_withdraw
                ELSE crystal_vault_users.last_withdraw
            END;
    """
    params = (
        vault.lower(),
        user_address.lower(),
        int(shares_delta or 0),
        int(deposits_delta or 0),
        int(withdraws_delta or 0),
        int(last_deposit) if last_deposit is not None else None,
        int(last_withdraw) if last_withdraw is not None else None,
        int(shares_delta or 0),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def upsert_crystal_vault_balance_sample(
    *,
    vault: str,
    block_number: int,
    timestamp: int,
    quote_balance: int,
    base_balance: int,
    usd_value,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_vault_balance_samples (
            vault, block_number, timestamp, quote_balance, base_balance, usd_value
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (vault, block_number) DO UPDATE SET
            timestamp = EXCLUDED.timestamp,
            quote_balance = EXCLUDED.quote_balance,
            base_balance = EXCLUDED.base_balance,
            usd_value = EXCLUDED.usd_value;
    """
    params = (
        vault.lower(),
        int(block_number),
        int(timestamp),
        int(quote_balance or 0),
        int(base_balance or 0),
        Decimal(str(usd_value or 0)),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def load_crystal_vaults_for_state():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                vault, quote, base, market, owner, name, description, social1, social2, social3,
                locked, closed, max_shares, circulating_shares, quote_decimals, base_decimals,
                lockup, decrease_on_withdraw
            FROM crystal_vaults
            """
        )
        return cur.fetchall()


def load_crystal_vault_users_for_state():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT vault, user_address, shares, deposits, withdraws, last_deposit, last_withdraw
            FROM crystal_vault_users
            """
        )
        return cur.fetchall()


def get_crystal_vault(vault: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                vault, quote, base, market, owner, name, description, social1, social2, social3,
                locked, closed, max_shares, circulating_shares, quote_decimals, base_decimals,
                lockup, decrease_on_withdraw
            FROM crystal_vaults
            WHERE vault = %s
            """,
            (vault.lower(),),
        )
        return cur.fetchone()


def get_crystal_vault_latest_balance(vault: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT block_number, timestamp, quote_balance, base_balance, usd_value
            FROM crystal_vault_balance_samples
            WHERE vault = %s
            ORDER BY timestamp DESC, block_number DESC
            LIMIT 1
            """,
            (vault.lower(),),
        )
        return cur.fetchone()


def get_crystal_vault_user(vault: str, user_address: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT shares, deposits, withdraws, last_deposit, last_withdraw
            FROM crystal_vault_users
            WHERE vault = %s AND user_address = %s
            """,
            (vault.lower(), user_address.lower()),
        )
        return cur.fetchone()


def list_crystal_vault_deposits(vault: str, limit: int = 50):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_address, timestamp, quote_amount, base_amount, shares, txhash
            FROM crystal_vault_deposits
            WHERE vault = %s
            ORDER BY timestamp DESC, log_index DESC
            LIMIT %s
            """,
            (vault.lower(), int(limit)),
        )
        return cur.fetchall()


def list_crystal_vault_withdrawals(vault: str, limit: int = 50):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_address, timestamp, quote_amount, base_amount, shares, txhash
            FROM crystal_vault_withdrawals
            WHERE vault = %s
            ORDER BY timestamp DESC, log_index DESC
            LIMIT %s
            """,
            (vault.lower(), int(limit)),
        )
        return cur.fetchall()


def list_crystal_vault_users(vault: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_address, shares, deposits, withdraws, last_deposit, last_withdraw
            FROM crystal_vault_users
            WHERE vault = %s
            ORDER BY last_deposit DESC, shares DESC, user_address ASC
            """,
            (vault.lower(),),
        )
        return cur.fetchall()


def list_crystal_vault_balance_samples(vault: str, start_ts: int | None = None, limit: int = 0):
    with db_cursor() as cur:
        if start_ts is None:
            if limit and limit > 0:
                cur.execute(
                    """
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value
                    FROM crystal_vault_balance_samples
                    WHERE vault = %s
                    ORDER BY timestamp DESC, block_number DESC
                    LIMIT %s
                    """,
                    (vault.lower(), int(limit)),
                )
            else:
                cur.execute(
                    """
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value
                    FROM crystal_vault_balance_samples
                    WHERE vault = %s
                    ORDER BY timestamp DESC, block_number DESC
                    """,
                    (vault.lower(),),
                )
        else:
            if limit and limit > 0:
                cur.execute(
                    """
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value
                    FROM crystal_vault_balance_samples
                    WHERE vault = %s AND timestamp >= %s
                    ORDER BY timestamp DESC, block_number DESC
                    LIMIT %s
                    """,
                    (vault.lower(), int(start_ts), int(limit)),
                )
            else:
                cur.execute(
                    """
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value
                    FROM crystal_vault_balance_samples
                    WHERE vault = %s AND timestamp >= %s
                    ORDER BY timestamp DESC, block_number DESC
                    """,
                    (vault.lower(), int(start_ts)),
                )
        rows = cur.fetchall()
    rows.reverse()
    return rows
