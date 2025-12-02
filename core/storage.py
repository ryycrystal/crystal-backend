from __future__ import annotations
from typing import Optional, Iterator
from contextlib import contextmanager
from decimal import Decimal

import os
import threading
import psycopg2

from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import Json, execute_values

_DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL", "postgresql://postgres:ShIsCu2024;1@localhost:5432/postgres")
_DB_MIN_CONN: int = 5
_DB_MAX_CONN: int = 125

_POOL: Optional[ThreadedConnectionPool] = None
_POOL_LOCK = threading.Lock()

# initializes global connection pool
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

# closes all connections in pool
def close_pool() -> None:
    global _POOL
    
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.closeall()
        _POOL = None

# internal helper, fetches pool or loudly fail
def _get_pool() -> ThreadedConnectionPool:
    global _POOL
    
    if _POOL is None:
        raise RuntimeError("[DB] Uninitialized connection pool")

    return _POOL

# yields a psycopg2 cursor from the pool, gets connection, creates cursor, 
# yields to caller, commits txn, closes cursor, returns connection to pool
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
    
# schema initialization
def init_db() -> None:
    with db_cursor() as cur:
        # extensions
        cur.execute(
            """
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            """
        )
        
        # processed blocks history
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
        
        # full trade history
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
        
        # tokens
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
        
        # user stats
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
        
        # positions
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
        
        # v3 pools
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
        
        # klines stuff
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
        
        # jolly portfolio
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
        
        # sniper stuff
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
        
        # mon price
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_meta
            (
                key   TEXT PRIMARY KEY,
                value NUMERIC(50, 18) NOT NULL
            );
            """
        )

# block helpers
def record_block_processed(block_number: int) -> None:
    with db_cursor() as cur:
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

# trade helpers
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

# tokens/pools   
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
    """Update token metadata for multiple tokens from async fetch results."""
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

# reload/state reconstruction      
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
        
# api helpers
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

# db helpers
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