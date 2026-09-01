from __future__ import annotations

from decimal import Decimal

import psycopg2

from .base import _clean_text, db_cursor


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


def set_crystal_vault_user_shares(
    *,
    vault: str,
    user_address: str,
    shares: int,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        UPDATE crystal_vault_users
        SET shares = GREATEST(%s, 0)
        WHERE vault = %s AND user_address = %s;
    """
    params = (int(shares or 0), vault.lower(), user_address.lower())
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
    shares: int | None = None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO crystal_vault_balance_samples (
            vault, block_number, timestamp, quote_balance, base_balance, usd_value, shares
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (vault, block_number) DO UPDATE SET
            timestamp = EXCLUDED.timestamp,
            quote_balance = EXCLUDED.quote_balance,
            base_balance = EXCLUDED.base_balance,
            usd_value = EXCLUDED.usd_value,
            shares = COALESCE(EXCLUDED.shares, crystal_vault_balance_samples.shares);
    """
    params = (
        vault.lower(),
        int(block_number),
        int(timestamp),
        int(quote_balance or 0),
        int(base_balance or 0),
        Decimal(str(usd_value or 0)),
        int(shares) if shares is not None else None,
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
            SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
            FROM crystal_vault_balance_samples
            WHERE vault = %s
            ORDER BY timestamp DESC, block_number DESC
            LIMIT 1
            """,
            (vault.lower(),),
        )
        return cur.fetchone()


def get_market_quote_ticker(market: str) -> str:
    if not market:
        return ""
    with db_cursor() as cur:
        cur.execute("SELECT COALESCE(quote_ticker, '') FROM crystal_markets WHERE market = %s", (market.lower(),))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else ""


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


def list_crystal_vaults_page(
    *,
    user_address: str | None = None,
    search: str | None = None,
    status: str = "all",
    page: int = 1,
    limit: int = 50,
    sort_by: str = "latest_deposit",
    sort_dir: str = "desc",
):
    page_i = max(1, int(page or 1))
    limit_i = max(1, min(int(limit or 50), 200))
    offset_i = (page_i - 1) * limit_i
    uaddr = (user_address or "").lower()
    q = (search or "").strip()
    q_like = f"%{q}%"
    status_norm = (status or "all").strip().lower()
    if status_norm not in {"all", "active", "closed"}:
        status_norm = "all"
    sort_norm = (sort_by or "latest_deposit").strip().lower()
    if sort_norm not in {"latest_deposit", "tvl", "user_position"}:
        sort_norm = "latest_deposit"
    dir_norm = (sort_dir or "desc").strip().lower()
    if dir_norm not in {"asc", "desc"}:
        dir_norm = "desc"
    status_clause = ""
    if status_norm == "active":
        status_clause = "AND v.closed = FALSE"
    elif status_norm == "closed":
        status_clause = "AND v.closed = TRUE"
    if sort_norm == "tvl":
        sort_expr = "COALESCE(lb.usd_value, 0.0)"
    elif sort_norm == "user_position":
        sort_expr = (
            "CASE WHEN COALESCE(v.circulating_shares, 0) > 0 "
            "THEN COALESCE(lb.usd_value, 0.0) * COALESCE(uv.shares, 0)::double precision / NULLIF(v.circulating_shares, 0)::double precision "
            "ELSE 0 END"
        )
    else:
        sort_expr = "COALESCE(ld.last_deposit, v.updated_at, v.deployed_at, 0)"
    order_dir = "ASC" if dir_norm == "asc" else "DESC"

    sql = f"""
        SELECT
            v.vault,
            v.owner,
            v.quote,
            v.base,
            v.market,
            v.name,
            v.description,
            v.social1,
            v.social2,
            v.social3,
            v.locked,
            v.closed,
            v.max_shares,
            v.circulating_shares,
            v.quote_decimals,
            v.base_decimals,
            v.lockup,
            v.decrease_on_withdraw,
            lb.block_number,
            lb.timestamp,
            lb.quote_balance,
            lb.base_balance,
            lb.usd_value,
            COALESCE(uv.shares, 0) AS user_shares,
            COALESCE(ld.last_deposit, 0) AS last_deposit,
            COALESCE(m.quote_ticker, '') AS quote_ticker,
            COALESCE(m.base_ticker, '') AS base_ticker,
            COALESCE(m.quote_name, '') AS quote_name,
            COALESCE(m.base_name, '') AS base_name,
            COUNT(*) OVER() AS total_count
        FROM crystal_vaults v
        LEFT JOIN LATERAL (
            SELECT block_number, timestamp, quote_balance, base_balance, usd_value
            FROM crystal_vault_balance_samples b
            WHERE b.vault = v.vault
            ORDER BY timestamp DESC, block_number DESC
            LIMIT 1
        ) lb ON TRUE
        LEFT JOIN LATERAL (
            SELECT shares
            FROM crystal_vault_users u
            WHERE u.vault = v.vault AND u.user_address = %s
            LIMIT 1
        ) uv ON TRUE
        LEFT JOIN LATERAL (
            SELECT timestamp AS last_deposit
            FROM crystal_vault_deposits d
            WHERE d.vault = v.vault
            ORDER BY timestamp DESC, log_index DESC
            LIMIT 1
        ) ld ON TRUE
        LEFT JOIN crystal_markets m
            ON m.market = v.market
        WHERE
            (
                %s = '' OR
                v.vault ILIKE %s OR
                v.owner ILIKE %s OR
                v.name ILIKE %s
            )
            {status_clause}
        ORDER BY
            {sort_expr} {order_dir} NULLS LAST,
            COALESCE(ld.last_deposit, v.updated_at, v.deployed_at, 0) DESC,
            v.vault ASC
        LIMIT %s OFFSET %s
    """

    params = (
        uaddr,
        q,
        q_like,
        q_like,
        q_like,
        limit_i,
        offset_i,
    )
    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


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


def list_crystal_vault_user_flows(vault: str, user: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, block_number, log_index, shares FROM crystal_vault_deposits
            WHERE vault = %s AND user_address = %s
            UNION ALL
            SELECT timestamp, block_number, log_index, -shares FROM crystal_vault_withdrawals
            WHERE vault = %s AND user_address = %s
            ORDER BY 1, 2, 3
            """,
            (vault.lower(), user.lower(), vault.lower(), user.lower()),
        )
        return [(int(r[0] or 0), int(r[3] or 0)) for r in cur.fetchall()]


def sum_vault_share_flows_after(vault: str, ts: int, inclusive: bool = False) -> tuple[int, int]:
    op = ">=" if inclusive else ">"
    with db_cursor() as cur:
        cur.execute(
            f"SELECT COALESCE(SUM(shares), 0) FROM crystal_vault_deposits WHERE vault = %s AND timestamp {op} %s",
            (vault.lower(), int(ts)),
        )
        minted = int(cur.fetchone()[0] or 0)
        cur.execute(
            f"SELECT COALESCE(SUM(shares), 0) FROM crystal_vault_withdrawals WHERE vault = %s AND timestamp {op} %s",
            (vault.lower(), int(ts)),
        )
        burned = int(cur.fetchone()[0] or 0)
    return minted, burned


def vault_ledger_divergences(limit: int = 50) -> list[tuple[str, str, int, int]]:
    """Depositors whose deposit/withdrawal ledger disagrees with their chain shares.

    crystal_vault_users.shares is reconciled against balanceOf by the sampler, but
    nothing validated the flow ledger those rows are derived from. A vault event
    dropped at index time (wrong or not-yet-configured factory address) leaves the
    ledger permanently ahead, and blocks are only processed once, so fixing the
    acceptance rule does not repair history. PnL reads the ledger, so the gap shows
    up as a position the holder no longer has.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT u.vault, u.user_address,
                   COALESCE(d.dep, 0) - COALESCE(w.wd, 0) AS ledger_net,
                   u.shares
            FROM crystal_vault_users u
            LEFT JOIN (
                SELECT vault, user_address, SUM(shares) dep
                FROM crystal_vault_deposits GROUP BY vault, user_address
            ) d ON d.vault = u.vault AND d.user_address = u.user_address
            LEFT JOIN (
                SELECT vault, user_address, SUM(shares) wd
                FROM crystal_vault_withdrawals GROUP BY vault, user_address
            ) w ON w.vault = u.vault AND w.user_address = u.user_address
            WHERE COALESCE(d.dep, 0) - COALESCE(w.wd, 0) <> u.shares
            ORDER BY ABS(COALESCE(d.dep, 0) - COALESCE(w.wd, 0) - u.shares) DESC
            LIMIT %s
            """,
            (int(limit),),
        )
        return [(str(a), str(b), int(c or 0), int(e or 0)) for a, b, c, e in cur.fetchall()]


def vault_sample_nav_before(vault: str, ts: int) -> tuple[int, float, int] | None:
    """Latest sample strictly before ts that can price a share on its own.

    The usd value and the share count come from the same on-chain read, so a NAV
    built from them cannot mix one moment's value with another moment's supply.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, usd_value, shares FROM crystal_vault_balance_samples
            WHERE vault = %s AND timestamp < %s AND usd_value > 0 AND shares > 0
            ORDER BY timestamp DESC LIMIT 1
            """,
            (vault.lower(), int(ts)),
        )
        row = cur.fetchone()
    return (int(row[0] or 0), float(row[1]), int(row[2])) if row else None


def vault_sample_nav_at_or_after(vault: str, ts: int) -> tuple[int, float, int] | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, usd_value, shares FROM crystal_vault_balance_samples
            WHERE vault = %s AND timestamp >= %s AND usd_value > 0 AND shares > 0
            ORDER BY timestamp ASC LIMIT 1
            """,
            (vault.lower(), int(ts)),
        )
        row = cur.fetchone()
    return (int(row[0] or 0), float(row[1]), int(row[2])) if row else None


def vault_sample_usd_before(vault: str, ts: int) -> tuple[int, float] | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, usd_value FROM crystal_vault_balance_samples
            WHERE vault = %s AND timestamp < %s AND usd_value > 0
            ORDER BY timestamp DESC LIMIT 1
            """,
            (vault.lower(), int(ts)),
        )
        row = cur.fetchone()
    return (int(row[0] or 0), float(row[1])) if row and row[1] is not None else None


def vault_sample_usd_at_or_after(vault: str, ts: int) -> tuple[int, float] | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, usd_value FROM crystal_vault_balance_samples
            WHERE vault = %s AND timestamp >= %s AND usd_value > 0
            ORDER BY timestamp ASC LIMIT 1
            """,
            (vault.lower(), int(ts)),
        )
        row = cur.fetchone()
    return (int(row[0] or 0), float(row[1])) if row and row[1] is not None else None


def vault_sample_window_stats(vault: str, start_ts: int | None = None):
    """min, max and latest usd plus the first and last rows that can price a share.

    the chart stats and the apy only ever needed these few numbers, but they were
    derived by shipping every sample in the window to python.
    """
    where = "vault = %s"
    params: list = [vault.lower()]
    if start_ts is not None:
        where += " AND timestamp >= %s"
        params.append(int(start_ts))
    with db_cursor() as cur:
        cur.execute(
            f"""
            WITH win AS (
                SELECT timestamp, block_number, quote_balance, base_balance, usd_value, shares
                FROM crystal_vault_balance_samples
                WHERE {where}
            ),
            priced AS (
                SELECT timestamp, quote_balance, base_balance, usd_value, shares FROM win
                WHERE shares > 0 AND usd_value > 0
            ),
            lo AS (SELECT * FROM priced ORDER BY timestamp ASC LIMIT 1),
            hi AS (SELECT * FROM priced ORDER BY timestamp DESC LIMIT 1)
            SELECT
                (SELECT MIN(usd_value) FROM win),
                (SELECT MAX(usd_value) FROM win),
                (SELECT COUNT(*) FROM win),
                (SELECT usd_value FROM win ORDER BY timestamp DESC, block_number DESC LIMIT 1),
                (SELECT timestamp FROM lo), (SELECT quote_balance FROM lo),
                (SELECT base_balance FROM lo), (SELECT usd_value FROM lo), (SELECT shares FROM lo),
                (SELECT timestamp FROM hi), (SELECT quote_balance FROM hi),
                (SELECT base_balance FROM hi), (SELECT usd_value FROM hi), (SELECT shares FROM hi)
            """,
            params,
        )
        return cur.fetchone()


def list_crystal_vault_sample_medians(vault: str, start_ts: int | None = None, buckets: int = 48):
    """Per-bucket median samples plus the true window endpoints.

    Mirrors _bucket_median_by_time: evenly spaced buckets, and within each the
    real row holding the median value (ties fall back to the earlier sample, the
    same way a stable sort by value does). Doing it here means the API no longer
    pulls every sample in the window just to keep a few dozen of them.
    """
    where = "vault = %s"
    params: list = [vault.lower()]
    if start_ts is not None:
        where += " AND timestamp >= %s"
        params.append(int(start_ts))
    n = int(max(1, buckets))
    with db_cursor() as cur:
        cur.execute(
            f"""
            WITH win AS (
                SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
                FROM crystal_vault_balance_samples
                WHERE {where}
            ),
            bounds AS (SELECT MIN(timestamp) AS mn, MAX(timestamp) AS mx FROM win),
            tagged AS (
                SELECT w.*,
                       LEAST(
                           GREATEST(((w.timestamp - b.mn) * {n}) / GREATEST(1, b.mx - b.mn), 0),
                           {n} - 1
                       ) AS bucket
                FROM win w CROSS JOIN bounds b
            ),
            ranked AS (
                SELECT t.*,
                       ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY usd_value, timestamp, block_number DESC) AS rn,
                       COUNT(*) OVER (PARTITION BY bucket) AS cnt
                FROM tagged t
            )
            SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
            FROM ranked
            WHERE rn = (cnt / 2) + 1
            ORDER BY timestamp
            """,
            params,
        )
        medians = cur.fetchall()

        cur.execute(
            f"""
            (SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
             FROM crystal_vault_balance_samples WHERE {where}
             ORDER BY timestamp ASC, block_number DESC LIMIT 1)
            UNION ALL
            (SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
             FROM crystal_vault_balance_samples WHERE {where}
             ORDER BY timestamp DESC, block_number ASC LIMIT 1)
            """,
            params + params,
        )
        ends = cur.fetchall()

    first_row = ends[0] if ends else None
    last_row = ends[1] if len(ends) > 1 else first_row
    return medians, first_row, last_row


def list_crystal_vault_balance_samples(vault: str, start_ts: int | None = None, limit: int = 0):
    with db_cursor() as cur:
        if start_ts is None:
            if limit and limit > 0:
                cur.execute(
                    """
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
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
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
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
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
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
                    SELECT block_number, timestamp, quote_balance, base_balance, usd_value, shares
                    FROM crystal_vault_balance_samples
                    WHERE vault = %s AND timestamp >= %s
                    ORDER BY timestamp DESC, block_number DESC
                    """,
                    (vault.lower(), int(start_ts)),
                )
        rows = cur.fetchall()
    rows.reverse()
    return rows


def vault_positions_usd(user) -> tuple[Decimal, list[dict]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT vu.vault, vu.shares, v.name, v.circulating_shares,
                   s.usd_value, s.shares AS sample_shares
            FROM crystal_vault_users vu
            JOIN crystal_vaults v ON v.vault = vu.vault
            LEFT JOIN LATERAL (
                SELECT usd_value, shares
                FROM crystal_vault_balance_samples b
                WHERE b.vault = vu.vault
                ORDER BY b.timestamp DESC, b.block_number DESC
                LIMIT 1
            ) s ON TRUE
            WHERE vu.user_address = ANY(%s) AND vu.shares > 0
            ORDER BY vu.vault
            """,
            ([user.lower()] if isinstance(user, str) else [str(u).lower() for u in (user or [])],),
        )
        rows = cur.fetchall()

    total = Decimal(0)
    folded: dict[str, dict] = {}
    for vault, shares, name, circulating, usd_value, sample_shares in rows:
        supply = Decimal(sample_shares or 0) or Decimal(circulating or 0)
        value = (Decimal(shares) / supply * Decimal(usd_value or 0)) if supply > 0 else None
        if value is not None:
            total += value
        prev = folded.get(vault)
        if prev is None:
            folded[vault] = {
                "vault": vault,
                "name": name or "",
                "shares": str(int(shares)),
                "valueUsd": value,
            }
        else:
            prev["shares"] = str(int(prev["shares"]) + int(shares))
            if value is not None:
                prev["valueUsd"] = (prev["valueUsd"] or Decimal(0)) + value
    return total, list(folded.values())
