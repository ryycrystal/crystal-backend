from __future__ import annotations

import math
from decimal import Decimal

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
            WHERE {" AND ".join(where)}
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


def list_pool_growth_events(market: str, lo_ts: int, hi_ts: int) -> list[tuple[int, float]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, prev_reserve_quote, prev_reserve_base, reserve_quote, reserve_base
            FROM crystal_pool_sync_events
            WHERE market = %s AND timestamp BETWEEN %s AND %s
              AND kind = 'trade'
              AND COALESCE(prev_reserve_quote, 0) > 0 AND COALESCE(prev_reserve_base, 0) > 0
              AND reserve_quote > 0 AND reserve_base > 0
            ORDER BY timestamp, block_number, log_index
            """,
            ((market or "").lower(), int(lo_ts), int(hi_ts)),
        )
        out = []
        for ts, prq, prb, rq, rb in cur.fetchall():
            k0 = float(prq) * float(prb)
            k1 = float(rq) * float(rb)
            if k0 > 0 and k1 > 0:
                out.append((int(ts), 0.5 * math.log(k1 / k0)))
        return out


def list_lp_positions(user: str) -> list[tuple[str, int, int]]:
    from .base import db_cursor

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT market, shares, last_transfer, cost_quote, cost_base
            FROM crystal_pool_lp_users
            WHERE user_address = %s AND shares > 0
            ORDER BY shares DESC
            """,
            ((user or "").lower(),),
        )
        return [
            ((m or "").lower(), int(s_ or 0), int(t or 0), int(cq or 0), int(cb or 0))
            for m, s_, t, cq, cb in cur.fetchall()
        ]


def list_lp_markets_for_graph(user: str) -> list[str]:
    addr = (user or "").lower()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT market FROM crystal_pool_lp_users WHERE user_address = %s
            UNION
            SELECT market FROM crystal_pool_liquidity_events WHERE user_address = %s
            ORDER BY market
            """,
            (addr, addr),
        )
        return [(row[0] or "").lower() for row in cur.fetchall() if row[0]]


def insert_pool_liquidity_event(
    *,
    txhash: str,
    log_index: int,
    market: str,
    kind: str,
    user_address: str,
    amount_quote: int,
    amount_base: int,
    block_number: int,
    timestamp: int,
    cur=None,
) -> None:
    from .base import db_cursor

    sql = """
        INSERT INTO crystal_pool_liquidity_events
            (txhash, log_index, market, kind, user_address, amount_quote, amount_base, block_number, timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (txhash, log_index) DO NOTHING
    """
    params = (
        (txhash or "").lower(),
        int(log_index),
        (market or "").lower(),
        kind,
        (user_address or "").lower(),
        int(amount_quote),
        int(amount_base),
        int(block_number),
        int(timestamp),
    )
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


def attach_pool_liquidity_shares(
    *, txhash: str, market: str, kind: str, shares: int, user_address: str = "", cur=None
) -> None:
    from .base import db_cursor

    sql = """
        UPDATE crystal_pool_liquidity_events
        SET shares = COALESCE(shares, 0) + %s,
            user_address = CASE WHEN COALESCE(user_address, '') = '' THEN %s ELSE user_address END
        WHERE txhash = %s AND market = %s AND kind = %s
    """
    params = (int(shares), (user_address or "").lower(), (txhash or "").lower(), (market or "").lower(), kind)
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


def list_pool_liquidity_events(market: str, user: str = "", limit: int = 100) -> list[dict]:
    from .base import db_cursor

    where = "market = %s"
    params: list = [(market or "").lower()]
    if user:
        where += " AND user_address = %s"
        params.append(user.lower())
    params.append(max(1, min(int(limit or 100), 500)))
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT txhash, log_index, kind, user_address, amount_quote, amount_base,
                   shares, block_number, timestamp
            FROM crystal_pool_liquidity_events
            WHERE {where}
            ORDER BY timestamp DESC, log_index DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [
        {
            "txHash": r[0],
            "logIndex": int(r[1]),
            "kind": r[2],
            "user": r[3] or None,
            "amountQuote": str(int(r[4] or 0)),
            "amountBase": str(int(r[5] or 0)),
            "shares": str(int(r[6])) if r[6] is not None else None,
            "block": int(r[7] or 0),
            "timestamp": int(r[8] or 0),
        }
        for r in rows
    ]


def apply_lp_cost_delta(
    *, market: str, user: str, dq: int, db_: int, burn_fraction_num: int = 0, burn_fraction_den: int = 0, cur=None
) -> None:
    from .base import db_cursor

    if burn_fraction_den > 0:
        sql = """
            UPDATE crystal_pool_lp_users
            SET cost_quote = cost_quote - (cost_quote * %s / %s),
                cost_base = cost_base - (cost_base * %s / %s)
            WHERE market = %s AND user_address = %s
        """
        params = (
            int(burn_fraction_num),
            int(burn_fraction_den),
            int(burn_fraction_num),
            int(burn_fraction_den),
            (market or "").lower(),
            (user or "").lower(),
        )
    else:
        sql = """
            INSERT INTO crystal_pool_lp_users (market, user_address, shares, cost_quote, cost_base)
            VALUES (%s, %s, 0, %s, %s)
            ON CONFLICT (market, user_address) DO UPDATE
            SET cost_quote = crystal_pool_lp_users.cost_quote + EXCLUDED.cost_quote,
                cost_base = crystal_pool_lp_users.cost_base + EXCLUDED.cost_base
        """
        params = ((market or "").lower(), (user or "").lower(), int(dq), int(db_))
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


def get_lp_user_shares(market: str, user: str, cur=None) -> int:
    from .base import db_cursor

    sql = "SELECT shares FROM crystal_pool_lp_users WHERE market = %s AND user_address = %s"
    params = ((market or "").lower(), (user or "").lower())
    if cur is not None:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    with db_cursor() as c:
        c.execute(sql, params)
        row = c.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_pool_liquidity_amounts(txhash: str, market: str, kind: str, cur=None) -> tuple[int, int] | None:
    from .base import db_cursor

    sql = """
        SELECT amount_quote, amount_base FROM crystal_pool_liquidity_events
        WHERE txhash = %s AND market = %s AND kind = %s
        ORDER BY log_index DESC LIMIT 1
    """
    params = ((txhash or "").lower(), (market or "").lower(), kind)
    if cur is not None:
        cur.execute(sql, params)
        row = cur.fetchone()
    else:
        with db_cursor() as c:
            c.execute(sql, params)
            row = c.fetchone()
    return (int(row[0] or 0), int(row[1] or 0)) if row else None


def crystal_pool_reserves_for_markets(markets: list[str], cur=None) -> dict[str, dict]:
    if not markets:
        return {}
    sql = """
        SELECT LOWER(market), reserve_quote, reserve_base, last_sync_at
        FROM crystal_pools
        WHERE LOWER(market) = ANY(%s) AND (reserve_quote > 0 OR reserve_base > 0)
    """
    args = ([m.lower() for m in markets if m],)
    if cur is None:
        with db_cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
    else:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return {
        m: {"reserveQuote": str(int(rq or 0)), "reserveBase": str(int(rb or 0)), "syncedAt": int(ts or 0)}
        for m, rq, rb, ts in rows
    }


def lp_positions_usd(user) -> tuple[Decimal, list[dict]]:
    users = [user.lower()] if isinstance(user, str) else [str(u).lower() for u in (user or [])]
    if not users:
        return Decimal(0), []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT lu.market, lu.shares, p.total_shares, p.tvl_usd,
                   m.base_ticker, m.quote_ticker
            FROM crystal_pool_lp_users lu
            JOIN crystal_pools p ON p.market = lu.market
            LEFT JOIN crystal_markets m ON m.market = lu.market
            WHERE lu.user_address = ANY(%s) AND lu.shares > 0
            ORDER BY lu.market
            """,
            (users,),
        )
        rows = cur.fetchall()

    total = Decimal(0)
    folded: dict[str, dict] = {}
    for market, shares, total_shares, tvl_usd, base_ticker, quote_ticker in rows:
        supply = Decimal(total_shares or 0)
        value = (Decimal(shares) / supply * Decimal(tvl_usd or 0)) if supply > 0 else None
        if value is not None:
            total += value
        prev = folded.get(market)
        if prev is None:
            folded[market] = {
                "market": market,
                "pair": f"{base_ticker or ''}/{quote_ticker or ''}".strip("/"),
                "shares": str(int(shares)),
                "valueUsd": value,
            }
        else:
            prev["shares"] = str(int(prev["shares"]) + int(shares))
            if value is not None:
                prev["valueUsd"] = (prev["valueUsd"] or Decimal(0)) + value
    return total, list(folded.values())
