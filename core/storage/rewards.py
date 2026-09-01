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
