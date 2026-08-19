from __future__ import annotations

from decimal import Decimal

import psycopg2

from .base import db_cursor


# load persisted lp pool state rows into in-memory state on startup
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


# fetch one lp pool state row by market address
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


# upsert current lp pool reserves and rolling metrics
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


# update only the lp token total supply for a pool
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


# insert a sync event with volume and fees
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


# sum trade-only sync volume and fees since a timestamp
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


# compute a time-weighted average tvl over a window using pool tvl samples
def time_weighted_avg_crystal_pool_tvl_since(
    market: str,
    since_ts: int,
    end_ts: int,
    cur: psycopg2.extensions.cursor | None = None,
):
    sql = """
        WITH p AS (
            SELECT %s::text AS market, %s::bigint AS since_ts, %s::bigint AS end_ts
        ),
        seed AS (
            SELECT s.timestamp, s.block_number, s.log_index, s.tvl_usd
            FROM crystal_pool_tvl_samples s, p
            WHERE s.market = p.market
              AND s.timestamp < p.since_ts
            ORDER BY s.timestamp DESC, s.block_number DESC, s.log_index DESC
            LIMIT 1
        ),
        inside_window AS (
            SELECT s.timestamp, s.block_number, s.log_index, s.tvl_usd
            FROM crystal_pool_tvl_samples s, p
            WHERE s.market = p.market
              AND s.timestamp >= p.since_ts
              AND s.timestamp <= p.end_ts
        ),
        pts AS (
            SELECT * FROM seed
            UNION ALL
            SELECT * FROM inside_window
        ),
        ord AS (
            SELECT
                timestamp,
                block_number,
                log_index,
                tvl_usd,
                LEAD(timestamp) OVER (ORDER BY timestamp ASC, block_number ASC, log_index ASC) AS next_ts
            FROM pts
        ),
        seg AS (
            SELECT
                tvl_usd,
                GREATEST(timestamp, (SELECT since_ts FROM p)) AS seg_start,
                LEAST(COALESCE(next_ts, (SELECT end_ts FROM p)), (SELECT end_ts FROM p)) AS seg_end
            FROM ord
        ),
        agg AS (
            SELECT
                COALESCE(SUM(tvl_usd * GREATEST(seg_end - seg_start, 0)), 0) AS weighted_sum,
                COALESCE(SUM(GREATEST(seg_end - seg_start, 0)), 0) AS total_secs
            FROM seg
        ),
        latest AS (
            SELECT s.tvl_usd
            FROM crystal_pool_tvl_samples s, p
            WHERE s.market = p.market
              AND s.timestamp <= p.end_ts
            ORDER BY s.timestamp DESC, s.block_number DESC, s.log_index DESC
            LIMIT 1
        )
        SELECT CASE
            WHEN (SELECT total_secs FROM agg) > 0
                THEN (SELECT weighted_sum FROM agg) / (SELECT total_secs FROM agg)
            ELSE COALESCE((SELECT tvl_usd FROM latest), 0)
        END
    """
    params = ((market or "").lower(), int(since_ts or 0), int(end_ts or 0))
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
            row = cur2.fetchone()
            return row[0] if row else 0
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else 0


# insert a pool tvl sample point for charting and averaging
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


# list recent pool tvl samples ordered oldest to newest for api charts
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
            WHERE {" AND ".join(where)}
            ORDER BY timestamp DESC, block_number DESC, log_index DESC
            LIMIT %s
        ) q
        ORDER BY timestamp ASC
    """
    with db_cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


# apply an lp transfer delta to a user's lp share balance for a pool
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


# list amm-enabled pools joined with current indexed state and metrics
def list_crystal_pools_with_state(
    *,
    search: str = "",
    token_addresses: list[str] | None = None,
    page: int = 1,
    limit: int = 50,
    sort_by: str = "volume",
    sort_dir: str = "desc",
):
    sort_key = (sort_by or "volume").strip().lower()
    if sort_key not in {"volume", "tvl", "apy"}:
        sort_key = "volume"
    dir_key = (sort_dir or "desc").strip().lower()
    if dir_key not in {"asc", "desc"}:
        dir_key = "desc"
    limit_i = max(1, min(int(limit or 50), 50))
    page_i = max(1, int(page or 1))
    offset_i = (page_i - 1) * limit_i
    order_sql = {
        "volume": "COALESCE(cp.volume_24h_usd, 0)",
        "tvl": "COALESCE(cp.tvl_usd, 0)",
        "apy": "COALESCE(cp.apy_24h, 0)",
    }[sort_key]
    search_s = (search or "").strip().lower()
    where_extra = ""
    params: list[object] = []
    toks = [str(t or "").lower() for t in (token_addresses or []) if str(t or "").strip()]
    if toks:
        toks = toks[:2]
        if len(toks) == 1:
            where_extra += """
              AND (
                    LOWER(cm.quote_address) = %s
                 OR LOWER(cm.base_address) = %s
              )
            """
            params.extend([toks[0], toks[0]])
        else:
            where_extra += """
              AND (
                    (LOWER(cm.quote_address) = %s AND LOWER(cm.base_address) = %s)
                 OR (LOWER(cm.quote_address) = %s AND LOWER(cm.base_address) = %s)
              )
            """
            params.extend([toks[0], toks[1], toks[1], toks[0]])
    if search_s:
        like = f"%{search_s}%"
        where_extra += """
              AND (
                    LOWER(cm.market) LIKE %s
                 OR LOWER(COALESCE(cm.quote_ticker, '')) LIKE %s
                 OR LOWER(COALESCE(cm.base_ticker, '')) LIKE %s
                 OR LOWER(COALESCE(cm.quote_name, '')) LIKE %s
                 OR LOWER(COALESCE(cm.base_name, '')) LIKE %s
                 OR LOWER(COALESCE(cm.base_ticker, '') || '/' || COALESCE(cm.quote_ticker, '')) LIKE %s
                 OR LOWER(COALESCE(cm.quote_ticker, '') || '/' || COALESCE(cm.base_ticker, '')) LIKE %s
              )
        """
        params.extend([like, like, like, like, like, like, like])
    with db_cursor() as cur:
        cur.execute(
            f"""
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
                COALESCE(cp.last_sync_at, 0),
                COUNT(*) OVER()
            FROM crystal_markets cm
            LEFT JOIN crystal_pools cp ON cp.market = cm.market
            WHERE cm.is_canonical = TRUE
              AND cm.market_type > 1
              AND cm.is_amm_enabled = TRUE
              {where_extra}
            ORDER BY {order_sql} {dir_key.upper()}, COALESCE(cp.updated_at, cm.updated_at, cm.created_at, 0) DESC, cm.market ASC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [limit_i, offset_i]),
        )
        return cur.fetchall()


# fetch one amm-enabled pool joined with current indexed state and metrics
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


# true lp yield for a window: the invariant sqrt(rq*rb) only grows on a swap by the
# fee actually kept, and swaps do not move shares, so compounding the per swap
# growth is per share yield by construction. fee x volume overstates it: a round
# trip pays the spread on a shrinking notional and price impact reverses to zero
def pool_invariant_growth_since(market: str, since_ts: int, cur=None) -> Decimal:
    sql = """
        SELECT prev_reserve_quote, prev_reserve_base, reserve_quote, reserve_base
        FROM crystal_pool_sync_events
        WHERE market = %s AND timestamp >= %s
          AND kind = 'trade'
          AND COALESCE(prev_reserve_quote, 0) > 0 AND COALESCE(prev_reserve_base, 0) > 0
          AND reserve_quote > 0 AND reserve_base > 0
        ORDER BY block_number, log_index
    """
    params = ((market or "").lower(), int(since_ts))
    if cur is not None:
        cur.execute(sql, params)
        rows = cur.fetchall()
    else:
        with db_cursor() as c:
            c.execute(sql, params)
            rows = c.fetchall()
    growth = Decimal(1)
    for prq, prb, rq, rb in rows:
        ratio = (Decimal(int(rq)) * Decimal(int(rb))) / (Decimal(int(prq)) * Decimal(int(prb)))
        if ratio > 0:
            growth *= ratio.sqrt()
    return growth - Decimal(1)


# fee events for a market in a window, for the historical apy series
def list_pool_fee_events(market: str, lo_ts: int, hi_ts: int) -> list[tuple[int, float]]:
    from .base import db_cursor

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, COALESCE(fees_usd, 0)
            FROM crystal_pool_sync_events
            WHERE market = %s AND timestamp BETWEEN %s AND %s AND COALESCE(fees_usd, 0) > 0
            ORDER BY timestamp
            """,
            ((market or "").lower(), int(lo_ts), int(hi_ts)),
        )
        return [(int(r[0]), float(r[1] or 0.0)) for r in cur.fetchall()]
