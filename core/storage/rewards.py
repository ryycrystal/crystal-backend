from __future__ import annotations

from .base import db_cursor


def ensure_rewards_tables(cur=None) -> None:
    if cur is None:
        with db_cursor() as c:
            ensure_rewards_tables(cur=c)
        return
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_contrib
        (
            week_start        BIGINT NOT NULL,
            wallet            TEXT NOT NULL,
            pregrad_usd       NUMERIC(50, 18) NOT NULL DEFAULT 0,
            grad_usd          NUMERIC(50, 18) NOT NULL DEFAULT 0,
            spot_taker_usd    NUMERIC(50, 18) NOT NULL DEFAULT 0,
            spot_maker_usd    NUMERIC(50, 18) NOT NULL DEFAULT 0,
            stable_taker_usd  NUMERIC(50, 18) NOT NULL DEFAULT 0,
            stable_maker_usd  NUMERIC(50, 18) NOT NULL DEFAULT 0,
            vault_usd_hours   NUMERIC(50, 18) NOT NULL DEFAULT 0,
            bonus_points      NUMERIC(50, 18) NOT NULL DEFAULT 0,
            points            NUMERIC(50, 18) NOT NULL DEFAULT 0,
            updated_at        BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (week_start, wallet)
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crystal_rewards_contrib_week_points
        ON crystal_rewards_contrib (week_start, points DESC);
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crystal_rewards_contrib_wallet
        ON crystal_rewards_contrib (wallet, week_start DESC);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_weeks
        (
            week_start     BIGINT PRIMARY KEY,
            week_end       BIGINT NOT NULL,
            pool           NUMERIC(50, 18) NOT NULL,
            exponent       NUMERIC(10, 6) NOT NULL,
            participants   BIGINT NOT NULL DEFAULT 0,
            total_raw      NUMERIC(50, 18) NOT NULL DEFAULT 0,
            total_adjusted NUMERIC(50, 18) NOT NULL DEFAULT 0,
            finalized      BOOLEAN NOT NULL DEFAULT FALSE,
            finalized_at   BIGINT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_distributions
        (
            week_start   BIGINT NOT NULL,
            wallet       TEXT NOT NULL,
            raw_points   NUMERIC(50, 18) NOT NULL,
            adjusted     NUMERIC(50, 18) NOT NULL,
            share        NUMERIC(20, 18) NOT NULL,
            crystals     NUMERIC(50, 18) NOT NULL,
            rank         BIGINT NOT NULL,
            participants BIGINT NOT NULL,
            status       TEXT NOT NULL,
            PRIMARY KEY (week_start, wallet)
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crystal_rewards_dist_wallet
        ON crystal_rewards_distributions (wallet, week_start DESC);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_balances
        (
            wallet     TEXT PRIMARY KEY,
            crystals   NUMERIC(50, 18) NOT NULL DEFAULT 0,
            updated_at BIGINT NOT NULL DEFAULT 0
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_grants
        (
            id       BIGSERIAL PRIMARY KEY,
            ts       BIGINT NOT NULL,
            wallet   TEXT NOT NULL,
            kind     TEXT NOT NULL,
            amount   NUMERIC(50, 18) NOT NULL,
            ref      TEXT NOT NULL DEFAULT ''
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crystal_rewards_grants_wallet
        ON crystal_rewards_grants (wallet, ts DESC);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_milestones
        (
            referee    TEXT NOT NULL,
            milestone  NUMERIC(50, 18) NOT NULL,
            granted_at BIGINT NOT NULL,
            referrer   TEXT NOT NULL,
            PRIMARY KEY (referee, milestone)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_campaigns
        (
            id         BIGSERIAL PRIMARY KEY,
            vault      TEXT NOT NULL,
            multiplier NUMERIC(20, 6) NOT NULL,
            start_ts   BIGINT NOT NULL,
            end_ts     BIGINT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_stable_tokens
        (
            address TEXT PRIMARY KEY
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_predeposit_vaults
        (
            vault TEXT PRIMARY KEY
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_denylist
        (
            wallet TEXT PRIMARY KEY
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_vault_gaps
        (
            hour        BIGINT NOT NULL,
            vault       TEXT NOT NULL,
            week_start  BIGINT NOT NULL,
            holders     BIGINT NOT NULL DEFAULT 0,
            shares      NUMERIC(78, 0) NOT NULL DEFAULT 0,
            last_sample BIGINT NOT NULL DEFAULT 0,
            reason      TEXT NOT NULL,
            PRIMARY KEY (hour, vault)
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crystal_rewards_vault_gaps_week
        ON crystal_rewards_vault_gaps (week_start, vault);
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crystal_rewards_leader
        (
            id           INTEGER PRIMARY KEY,
            holder       TEXT,
            heartbeat_at TIMESTAMPTZ
        );
        """
    )
    cur.execute(
        """
        INSERT INTO crystal_rewards_leader (id, holder, heartbeat_at)
        VALUES (1, NULL, NULL)
        ON CONFLICT (id) DO NOTHING;
        """
    )
    cur.execute(
        """
        INSERT INTO crystal_rewards_stable_tokens (address) VALUES
            ('0x754704bc059f8c67012fed69bc8a327a5aafb603'),
            ('0x00000000efe302beaa2b3e6e1b18d08d69a9012a')
        ON CONFLICT (address) DO NOTHING;
        """
    )


def claim_rewards_leader(holder: str, ttl_seconds: int = 90) -> bool:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE crystal_rewards_leader
            SET holder = %s, heartbeat_at = Now()
            WHERE id = 1
              AND (holder = %s OR holder IS NULL OR heartbeat_at < Now() - make_interval(secs => %s))
            RETURNING holder
            """,
            (holder, holder, int(ttl_seconds)),
        )
        return cur.fetchone() is not None


def rewards_stable_tokens(cur) -> set[str]:
    cur.execute("SELECT address FROM crystal_rewards_stable_tokens")
    return {str(r[0]).lower() for r in cur.fetchall()}


def rewards_predeposit_vaults(cur) -> set[str]:
    cur.execute("SELECT vault FROM crystal_rewards_predeposit_vaults")
    return {str(r[0]).lower() for r in cur.fetchall()}


def rewards_denylist(cur) -> set[str]:
    cur.execute("SELECT wallet FROM crystal_rewards_denylist")
    return {str(r[0]).lower() for r in cur.fetchall()}


def rewards_campaigns(cur) -> list[tuple[str, float, int, int]]:
    cur.execute("SELECT vault, multiplier, start_ts, end_ts FROM crystal_rewards_campaigns")
    return [(str(r[0]).lower(), float(r[1]), int(r[2]), int(r[3])) for r in cur.fetchall()]


def add_rewards_contrib(cur, week_start: int, wallet: str, now_ts: int, **deltas) -> None:
    cols = (
        "pregrad_usd",
        "grad_usd",
        "spot_taker_usd",
        "spot_maker_usd",
        "stable_taker_usd",
        "stable_maker_usd",
        "vault_usd_hours",
        "bonus_points",
        "points",
    )
    values = [float(deltas.get(c, 0) or 0) for c in cols]
    if not any(values):
        return
    sets = ", ".join(f"{c} = crystal_rewards_contrib.{c} + EXCLUDED.{c}" for c in cols)
    cur.execute(
        f"""
        INSERT INTO crystal_rewards_contrib
            (week_start, wallet, {", ".join(cols)}, updated_at)
        VALUES (%s, %s, {", ".join("%s" for _ in cols)}, %s)
        ON CONFLICT (week_start, wallet) DO UPDATE
        SET {sets}, updated_at = EXCLUDED.updated_at
        """,
        (int(week_start), wallet.lower(), *values, int(now_ts)),
    )


def add_rewards_balance(cur, wallet: str, amount, now_ts: int) -> None:
    cur.execute(
        """
        INSERT INTO crystal_rewards_balances (wallet, crystals, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (wallet) DO UPDATE
        SET crystals = crystal_rewards_balances.crystals + EXCLUDED.crystals,
            updated_at = EXCLUDED.updated_at
        """,
        (wallet.lower(), amount, int(now_ts)),
    )


def get_rewards_balance(cur, wallet: str) -> float:
    cur.execute("SELECT crystals FROM crystal_rewards_balances WHERE wallet = %s", (wallet.lower(),))
    row = cur.fetchone()
    return float(row[0]) if row else 0.0


def add_rewards_denylist(addresses: list[str]) -> list[str]:
    rows = sorted({str(a or "").lower() for a in addresses if str(a or "").strip()})
    if not rows:
        return []
    with db_cursor() as cur:
        cur.executemany(
            "INSERT INTO crystal_rewards_denylist (wallet) VALUES (%s) ON CONFLICT (wallet) DO NOTHING",
            [(a,) for a in rows],
        )
    return rows


def remove_rewards_denylist(address: str) -> bool:
    with db_cursor() as cur:
        cur.execute("DELETE FROM crystal_rewards_denylist WHERE wallet = %s", (str(address or "").lower(),))
        return cur.rowcount > 0


def list_rewards_denylist() -> list[str]:
    with db_cursor() as cur:
        cur.execute("SELECT wallet FROM crystal_rewards_denylist ORDER BY wallet")
        return [str(r[0]) for r in cur.fetchall()]


def add_predeposit_vaults(vaults: list[str]) -> list[str]:
    rows = sorted({str(v or "").lower() for v in vaults if str(v or "").strip()})
    if not rows:
        return []
    with db_cursor() as cur:
        cur.executemany(
            "INSERT INTO crystal_rewards_predeposit_vaults (vault) VALUES (%s) ON CONFLICT (vault) DO NOTHING",
            [(v,) for v in rows],
        )
    return rows


def remove_predeposit_vault(vault: str) -> bool:
    with db_cursor() as cur:
        cur.execute("DELETE FROM crystal_rewards_predeposit_vaults WHERE vault = %s", (str(vault or "").lower(),))
        return cur.rowcount > 0


def list_predeposit_vaults() -> list[str]:
    with db_cursor() as cur:
        cur.execute("SELECT vault FROM crystal_rewards_predeposit_vaults ORDER BY vault")
        return [str(r[0]) for r in cur.fetchall()]


def record_vault_gap(
    cur,
    hour: int,
    vault: str,
    week_start: int,
    holders: int,
    shares,
    last_sample: int,
    reason: str,
) -> bool:
    cur.execute(
        """
        INSERT INTO crystal_rewards_vault_gaps
            (hour, vault, week_start, holders, shares, last_sample, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (hour, vault) DO UPDATE
        SET week_start = EXCLUDED.week_start, holders = EXCLUDED.holders,
            shares = EXCLUDED.shares, last_sample = EXCLUDED.last_sample,
            reason = EXCLUDED.reason
        WHERE crystal_rewards_vault_gaps.reason IS DISTINCT FROM EXCLUDED.reason
           OR crystal_rewards_vault_gaps.last_sample IS DISTINCT FROM EXCLUDED.last_sample
           OR crystal_rewards_vault_gaps.holders IS DISTINCT FROM EXCLUDED.holders
        """,
        (int(hour), str(vault).lower(), int(week_start), int(holders), shares, int(last_sample), reason),
    )
    return cur.rowcount > 0


def clear_vault_gap(cur, hour: int, vault: str) -> None:
    cur.execute(
        "DELETE FROM crystal_rewards_vault_gaps WHERE hour = %s AND vault = %s",
        (int(hour), str(vault).lower()),
    )


def worst_vault_gap(cur, week_start: int) -> tuple[str, int]:
    cur.execute(
        """
        SELECT vault, COUNT(*) AS hours FROM crystal_rewards_vault_gaps
        WHERE week_start = %s GROUP BY vault ORDER BY hours DESC, vault LIMIT 1
        """,
        (int(week_start),),
    )
    row = cur.fetchone()
    return (str(row[0]), int(row[1])) if row else ("", 0)


def vault_gaps(cur, week_start: int | None = None, limit: int = 200) -> list[dict]:
    sql = """
        SELECT vault, week_start, COUNT(*) AS hours, MIN(hour), MAX(hour),
               MAX(holders), MAX(last_sample),
               (ARRAY_AGG(reason ORDER BY hour DESC))[1]
        FROM crystal_rewards_vault_gaps
    """
    params: tuple = ()
    if week_start is not None:
        sql += " WHERE week_start = %s"
        params = (int(week_start),)
    sql += " GROUP BY vault, week_start ORDER BY hours DESC, vault LIMIT %s"
    cur.execute(sql, params + (max(1, min(int(limit), 1000)),))
    return [
        {
            "vault": str(v),
            "weekStart": int(ws),
            "hours": int(n),
            "firstHour": int(lo),
            "lastHour": int(hi),
            "holders": int(h or 0),
            "lastSample": int(ls or 0),
            "reason": str(r),
        }
        for v, ws, n, lo, hi, h, ls, r in cur.fetchall()
    ]
