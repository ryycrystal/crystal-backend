from __future__ import annotations

import psycopg2

from .base import db_cursor


def upsert_referral_binding(
    referee: str,
    referrer: str,
    block_number: int,
    log_index: int,
    timestamp: int,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    sql = """
        INSERT INTO referral_bindings (referee, referrer, block_number, log_index, timestamp)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (referee) DO UPDATE SET
            referrer = EXCLUDED.referrer,
            block_number = EXCLUDED.block_number,
            log_index = EXCLUDED.log_index,
            timestamp = EXCLUDED.timestamp
        WHERE (EXCLUDED.block_number, EXCLUDED.log_index)
            >= (referral_bindings.block_number, referral_bindings.log_index)
    """
    params = ((referee or "").lower(), (referrer or "").lower(), int(block_number), int(log_index), int(timestamp))
    if cur is not None:
        cur.execute(sql, params)
        return
    with db_cursor() as c:
        c.execute(sql, params)


def get_referral_binding(referee: str):
    with db_cursor() as cur:
        cur.execute(
            "SELECT referrer, block_number, timestamp FROM referral_bindings WHERE referee = %s",
            ((referee or "").lower(),),
        )
        return cur.fetchone()


def list_referees(referrer: str, limit: int = 0):
    with db_cursor() as cur:
        if limit and limit > 0:
            cur.execute(
                """
                SELECT referee, timestamp FROM referral_bindings
                WHERE referrer = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                ((referrer or "").lower(), int(limit)),
            )
        else:
            cur.execute(
                """
                SELECT referee, timestamp FROM referral_bindings
                WHERE referrer = %s
                ORDER BY timestamp DESC
                """,
                ((referrer or "").lower(),),
            )
        return cur.fetchall()


def list_active_referrers() -> list[str]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT referrer FROM referral_bindings
            WHERE referrer <> '0x0000000000000000000000000000000000000000'
            """
        )
        return [str(r[0]) for r in cur.fetchall()]


def record_referral_reward(referrer: str, token: str, claimable: int, ts: int) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO referral_rewards (referrer, token, claimable, earned, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (referrer, token) DO UPDATE SET
                earned = referral_rewards.earned
                    + GREATEST(EXCLUDED.claimable - referral_rewards.claimable, 0),
                claimable = EXCLUDED.claimable,
                updated_at = EXCLUDED.updated_at
            WHERE EXCLUDED.updated_at >= referral_rewards.updated_at
            """,
            ((referrer or "").lower(), (token or "").lower(), int(claimable), int(claimable), int(ts)),
        )


def get_referral_rewards(referrer: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT token, claimable, earned, updated_at FROM referral_rewards
            WHERE referrer = %s
            ORDER BY token
            """,
            ((referrer or "").lower(),),
        )
        return cur.fetchall()


def list_volume_tiers():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT tier, name, min_volume_usd, cashback_multiplier, referral_commission_bps
            FROM volume_tiers
            ORDER BY min_volume_usd ASC, tier ASC
            """
        )
        return cur.fetchall()


def tier_window_days(default: int = 30) -> int:
    with db_cursor() as cur:
        cur.execute("SELECT value FROM launchpad_kv WHERE key = 'tier_volume_window_days'")
        row = cur.fetchone()
    if not row:
        return default
    try:
        return max(int(str(row[0]).strip()), 0)
    except (TypeError, ValueError):
        return default


def wallet_launchpad_volume_usd(address: str, since_ts: int = 0):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(usd_amount), 0), COUNT(*)
            FROM launchpad_trades
            WHERE user_address = %s AND timestamp >= %s
            """,
            ((address or "").lower(), int(since_ts or 0)),
        )
        row = cur.fetchone()
    return (row[0] if row else 0), int(row[1] if row else 0)
