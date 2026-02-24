from __future__ import annotations

from decimal import Decimal
from typing import Optional

import psycopg2

from .base import db_cursor


def load_crystal_pool_states_for_state():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market,
                reserve_quote,
                reserve_base,
                total_shares,
                tvl_usd,
                volume_24h_usd,
                fees_24h_usd,
                apy_24h,
                daily_yield_24h,
                last_sync_block,
                last_sync_at
            FROM crystal_pools
            """
        )
        return cur.fetchall()


def get_crystal_pool_state(market: str, cur: psycopg2.extensions.cursor | None = None):
    sql = """
        SELECT
            market,
            reserve_quote,
            reserve_base,
            total_shares,
            tvl_usd,
            volume_24h_usd,
            fees_24h_usd,
            apy_24h,
            daily_yield_24h,
            last_sync_block,
            last_sync_at
        FROM crystal_pools
        WHERE market = %s
        LIMIT 1
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, ((market or "").lower(),))
            return cur2.fetchone()
    cur.execute(sql, ((market or "").lower(),))
    return cur.fetchone()


def upsert_crystal_pool_state(
    *,
    market: str,
    reserve_quote: int,
    reserve_base: int,
    total_shares: int | None = None,
    tvl_usd,
    volume_24h_usd,
    fees_24h_usd,
    apy_24h,
    daily_yield_24h,
    last_sync_block: int | None,
    last_sync_at: int | None,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_pools (
            market,
            reserve_quote,
            reserve_base,
            total_shares,
            tvl_usd,
            volume_24h_usd,
            fees_24h_usd,
            apy_24h,
            daily_yield_24h,
            last_sync_block,
            last_sync_at,
            updated_block,
            updated_at
        )
        VALUES (
            %s, %s, %s, COALESCE(%s, 0), %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (market) DO UPDATE SET
            reserve_quote = EXCLUDED.reserve_quote,
            reserve_base = EXCLUDED.reserve_base,
            total_shares = CASE
                WHEN %s IS NULL THEN crystal_pools.total_shares
                ELSE EXCLUDED.total_shares
            END,
            tvl_usd = EXCLUDED.tvl_usd,
            volume_24h_usd = EXCLUDED.volume_24h_usd,
            fees_24h_usd = EXCLUDED.fees_24h_usd,
            apy_24h = EXCLUDED.apy_24h,
            daily_yield_24h = EXCLUDED.daily_yield_24h,
            last_sync_block = COALESCE(EXCLUDED.last_sync_block, crystal_pools.last_sync_block),
            last_sync_at = COALESCE(EXCLUDED.last_sync_at, crystal_pools.last_sync_at),
            updated_block = COALESCE(EXCLUDED.updated_block, crystal_pools.updated_block),
            updated_at = COALESCE(EXCLUDED.updated_at, crystal_pools.updated_at);
    """
    params = (
        (market or "").lower(),
        int(reserve_quote or 0),
        int(reserve_base or 0),
        None if total_shares is None else int(total_shares or 0),
        Decimal(str(tvl_usd or 0)),
        Decimal(str(volume_24h_usd or 0)),
        Decimal(str(fees_24h_usd or 0)),
        Decimal(str(apy_24h or 0)),
        Decimal(str(daily_yield_24h or 0)),
        int(last_sync_block) if last_sync_block is not None else None,
        int(last_sync_at) if last_sync_at is not None else None,
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        None if total_shares is None else int(total_shares or 0),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def update_crystal_pool_total_shares(
    market: str,
    total_shares: int,
    updated_block: int | None = None,
    updated_at: int | None = None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_pools (market, total_shares, updated_block, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (market) DO UPDATE SET
            total_shares = EXCLUDED.total_shares,
            updated_block = COALESCE(EXCLUDED.updated_block, crystal_pools.updated_block),
            updated_at = COALESCE(EXCLUDED.updated_at, crystal_pools.updated_at);
    """
    params = (
        (market or "").lower(),
        int(total_shares or 0),
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def insert_crystal_pool_sync_event(
    *,
    block_number: int,
    log_index: int,
    timestamp: int,
    market: str,
    txhash: str,
    kind: str,
    reserve_quote: int,
    reserve_base: int,
    prev_reserve_quote: int | None,
    prev_reserve_base: int | None,
    volume_usd,
    fees_usd,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_pool_sync_events (
            block_number,
            log_index,
            timestamp,
            market,
            txhash,
            kind,
            reserve_quote,
            reserve_base,
            prev_reserve_quote,
            prev_reserve_base,
            volume_usd,
            fees_usd
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING;
    """
    params = (
        int(block_number or 0),
        int(log_index or 0),
        int(timestamp or 0),
        (market or "").lower(),
        (txhash or "").lower(),
        str(kind or "other"),
        int(reserve_quote or 0),
        int(reserve_base or 0),
        int(prev_reserve_quote) if prev_reserve_quote is not None else None,
        int(prev_reserve_base) if prev_reserve_base is not None else None,
        Decimal(str(volume_usd or 0)),
        Decimal(str(fees_usd or 0)),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def sum_crystal_pool_trade_metrics_since(
    market: str,
    since_ts: int,
    cur: psycopg2.extensions.cursor | None = None,
):
    sql = """
        SELECT
            COALESCE(SUM(volume_usd), 0),
            COALESCE(SUM(fees_usd), 0)
        FROM crystal_pool_sync_events
        WHERE market = %s
          AND kind = 'trade'
          AND timestamp >= %s
    """
    params = ((market or "").lower(), int(since_ts or 0))
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
            return cur2.fetchone()
    cur.execute(sql, params)
    return cur.fetchone()


def insert_crystal_pool_tvl_sample(
    *,
    market: str,
    block_number: int,
    log_index: int,
    timestamp: int,
    reserve_quote: int,
    reserve_base: int,
    tvl_usd,
    txhash: str,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_pool_tvl_samples (
            market,
            block_number,
            log_index,
            timestamp,
            reserve_quote,
            reserve_base,
            tvl_usd,
            txhash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market, block_number, log_index) DO UPDATE SET
            timestamp = EXCLUDED.timestamp,
            reserve_quote = EXCLUDED.reserve_quote,
            reserve_base = EXCLUDED.reserve_base,
            tvl_usd = EXCLUDED.tvl_usd,
            txhash = EXCLUDED.txhash;
    """
    params = (
        (market or "").lower(),
        int(block_number or 0),
        int(log_index or 0),
        int(timestamp or 0),
        int(reserve_quote or 0),
        int(reserve_base or 0),
        Decimal(str(tvl_usd or 0)),
        (txhash or "").lower(),
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def list_crystal_pool_tvl_samples(
    market: str,
    *,
    since_ts: int | None = None,
    limit: int = 500,
):
    params: list[object] = [(market or "").lower()]
    where = ["market = %s"]
    if since_ts is not None:
        where.append("timestamp >= %s")
        params.append(int(since_ts))
    limit_i = max(1, min(int(limit or 500), 5000))
    params.append(limit_i)
    sql = f"""
        SELECT timestamp, tvl_usd
        FROM (
            SELECT timestamp, tvl_usd
            FROM crystal_pool_tvl_samples
            WHERE {' AND '.join(where)}
            ORDER BY timestamp DESC, block_number DESC, log_index DESC
            LIMIT %s
        ) q
        ORDER BY timestamp ASC
    """
    with db_cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


def upsert_crystal_pool_lp_user_delta(
    *,
    market: str,
    user_address: str,
    shares_delta: int,
    last_transfer: int | None,
    updated_block: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_pool_lp_users (
            market,
            user_address,
            shares,
            last_transfer,
            updated_block
        )
        VALUES (%s, %s, GREATEST(%s::numeric, 0), COALESCE(%s, 0), %s)
        ON CONFLICT (market, user_address) DO UPDATE SET
            shares = GREATEST(0, crystal_pool_lp_users.shares + %s::numeric),
            last_transfer = GREATEST(crystal_pool_lp_users.last_transfer, COALESCE(%s, 0)),
            updated_block = COALESCE(%s, crystal_pool_lp_users.updated_block)
    """
    last_transfer_i = int(last_transfer) if last_transfer is not None else None
    updated_block_i = int(updated_block) if updated_block is not None else None
    shares_delta_i = int(shares_delta or 0)
    params = (
        (market or "").lower(),
        (user_address or "").lower(),
        shares_delta_i,
        last_transfer_i,
        updated_block_i,
        shares_delta_i,
        last_transfer_i,
        updated_block_i,
    )
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def list_crystal_pools_with_state():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                cm.market,
                cm.quote_address,
                cm.base_address,
                cm.market_type,
                cm.quote_decimals,
                cm.base_decimals,
                cm.quote_ticker,
                cm.quote_name,
                cm.base_ticker,
                cm.base_name,
                cm.taker_fee,
                cm.is_amm_enabled,
                cm.last_price,
                cm.updated_at,
                cm.created_at,
                COALESCE(cp.reserve_quote, 0),
                COALESCE(cp.reserve_base, 0),
                COALESCE(cp.total_shares, 0),
                COALESCE(cp.tvl_usd, 0),
                COALESCE(cp.volume_24h_usd, 0),
                COALESCE(cp.fees_24h_usd, 0),
                COALESCE(cp.apy_24h, 0),
                COALESCE(cp.daily_yield_24h, 0),
                COALESCE(cp.last_sync_block, 0),
                COALESCE(cp.last_sync_at, 0)
            FROM crystal_markets cm
            LEFT JOIN crystal_pools cp ON cp.market = cm.market
            WHERE cm.is_canonical = TRUE
              AND cm.market_type > 1
              AND cm.is_amm_enabled = TRUE
            ORDER BY COALESCE(cp.tvl_usd, 0) DESC, COALESCE(cp.updated_at, cm.updated_at, cm.created_at, 0) DESC, cm.market ASC
            """
        )
        return cur.fetchall()


def get_crystal_pool_with_state(market: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                cm.market,
                cm.quote_address,
                cm.base_address,
                cm.market_type,
                cm.quote_decimals,
                cm.base_decimals,
                cm.quote_ticker,
                cm.quote_name,
                cm.base_ticker,
                cm.base_name,
                cm.taker_fee,
                cm.is_amm_enabled,
                cm.last_price,
                cm.updated_at,
                cm.created_at,
                COALESCE(cp.reserve_quote, 0),
                COALESCE(cp.reserve_base, 0),
                COALESCE(cp.total_shares, 0),
                COALESCE(cp.tvl_usd, 0),
                COALESCE(cp.volume_24h_usd, 0),
                COALESCE(cp.fees_24h_usd, 0),
                COALESCE(cp.apy_24h, 0),
                COALESCE(cp.daily_yield_24h, 0),
                COALESCE(cp.last_sync_block, 0),
                COALESCE(cp.last_sync_at, 0)
            FROM crystal_markets cm
            LEFT JOIN crystal_pools cp ON cp.market = cm.market
            WHERE cm.is_canonical = TRUE
              AND cm.market_type > 1
              AND cm.is_amm_enabled = TRUE
              AND cm.market = %s
            LIMIT 1
            """,
            ((market or "").lower(),),
        )
        return cur.fetchone()
