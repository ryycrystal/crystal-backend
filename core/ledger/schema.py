from __future__ import annotations

from core.ledger.types import EFFECT_COLUMNS

LEDGER_TABLES = (
    "wallet_flows",
    "positions_v2",
    "address_kinds",
    "venues",
    "token_registry",
    "token_coverage",
    "token_fold_state",
    "parked_entitlements",
    "tx_meta",
    "tx_traces",
    "ledger_meta",
)

FLOW_EFFECT_DDL = tuple(f"{column} NUMERIC(78, 0) NOT NULL DEFAULT 0" for column in EFFECT_COLUMNS) + (
    "interpretation INTEGER NOT NULL DEFAULT 0",
)
POSITION_INVENTORY_DDL = (
    "observed_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "estimated_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "parked_observed_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "parked_estimated_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "parked_unresolved_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "parked_observed_basis NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "parked_estimated_basis NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "disposed_unresolved_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "disposed_unresolved_basis_native NUMERIC(78, 0) NOT NULL DEFAULT 0",
    "last_trade_tx TEXT",
    "last_buy_tx TEXT",
    "last_sell_tx TEXT",
)
ADDED_COLUMNS = (("wallet_flows", FLOW_EFFECT_DDL), ("positions_v2", POSITION_INVENTORY_DDL))


def _columns(ddl: tuple[str, ...]) -> str:
    return "".join(f",\n        {line}" for line in ddl)


_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS wallet_flows
    (
        block_number   BIGINT NOT NULL,
        tx_index       INTEGER NOT NULL,
        log_index      INTEGER NOT NULL,
        sub_index      INTEGER NOT NULL,
        txhash         TEXT NOT NULL,
        timestamp      BIGINT NOT NULL,
        wallet         TEXT NOT NULL,
        token          TEXT NOT NULL,
        token_delta    NUMERIC(78, 0) NOT NULL,
        quote_asset    TEXT,
        quote_delta    NUMERIC(78, 0),
        mon_value      NUMERIC(50, 18) NOT NULL DEFAULT 0,
        usd_value      NUMERIC(50, 18) NOT NULL DEFAULT 0,
        kind           TEXT NOT NULL,
        venue          TEXT,
        counterparty   TEXT,
        origin         TEXT,
        source         TEXT NOT NULL,
        basis_state    TEXT NOT NULL,
        price_native   NUMERIC(50, 18),
        basis_delta    NUMERIC(78, 0) NOT NULL DEFAULT 0,
        realized_delta NUMERIC(78, 0) NOT NULL DEFAULT 0{_columns(FLOW_EFFECT_DDL)},
        PRIMARY KEY (block_number, tx_index, log_index, sub_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wallet_flows_wallet_token_pos
    ON wallet_flows (wallet, token, block_number, tx_index, log_index, sub_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wallet_flows_token_block
    ON wallet_flows (token, block_number)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS positions_v2
    (
        wallet                     TEXT NOT NULL,
        token                      TEXT NOT NULL,
        balance_token              NUMERIC(78, 0),
        custody_balance            NUMERIC(78, 0),
        token_bought               NUMERIC(78, 0),
        token_sold                 NUMERIC(78, 0),
        native_spent               NUMERIC(78, 0),
        native_received            NUMERIC(78, 0),
        cost_basis_native          NUMERIC(78, 0),
        realized_pnl_native        NUMERIC(78, 0),
        basis_estimated_native     NUMERIC(78, 0),
        realized_estimated_native  NUMERIC(78, 0),
        unresolved_tokens          NUMERIC(78, 0),
        unresolved_proceeds_native NUMERIC(78, 0),
        trade_count                INTEGER,
        buy_count                  INTEGER,
        sell_count                 INTEGER,
        first_flow_ts              BIGINT,
        last_flow_ts               BIGINT,
        last_flow_block            BIGINT,
        flow_count                 INTEGER{_columns(POSITION_INVENTORY_DDL)},
        PRIMARY KEY (wallet, token)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS address_kinds
    (
        address          TEXT PRIMARY KEY,
        kind             TEXT NOT NULL,
        source           TEXT NOT NULL,
        first_seen_block BIGINT,
        evidence         JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venues
    (
        address     TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        token0      TEXT,
        token1      TEXT,
        discovered  BOOLEAN NOT NULL DEFAULT FALSE,
        reviewed_at BIGINT,
        evidence    JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_registry
    (
        token            TEXT PRIMARY KEY,
        source           TEXT NOT NULL,
        registered_block BIGINT,
        quote_token      TEXT,
        decimals         INTEGER NOT NULL DEFAULT 18,
        active           BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_fold_state
    (
        token          TEXT PRIMARY KEY,
        folded_through BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parked_entitlements
    (
        wallet            TEXT NOT NULL,
        token             TEXT NOT NULL,
        venue             TEXT NOT NULL,
        observed_tokens   NUMERIC(78, 0) NOT NULL DEFAULT 0,
        estimated_tokens  NUMERIC(78, 0) NOT NULL DEFAULT 0,
        unresolved_tokens NUMERIC(78, 0) NOT NULL DEFAULT 0,
        observed_basis    NUMERIC(78, 0) NOT NULL DEFAULT 0,
        estimated_basis   NUMERIC(78, 0) NOT NULL DEFAULT 0,
        PRIMARY KEY (wallet, token, venue)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_coverage
    (
        token      TEXT NOT NULL,
        from_block BIGINT NOT NULL,
        to_block   BIGINT NOT NULL,
        PRIMARY KEY (token, from_block)
    )
    """,
    """
    CREATE OR REPLACE VIEW ledger_token_coverage AS
    WITH spans AS (
        SELECT r.token, r.registered_block, c.from_block, c.to_block
        FROM token_registry r
        JOIN token_coverage c ON c.token = r.token OR c.token = '*'
    ),
    edges AS (
        SELECT token, registered_block, from_block, to_block,
               MAX(to_block) OVER (
                   PARTITION BY token ORDER BY from_block, to_block
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
               ) AS reach
        FROM spans
    ),
    islands AS (
        SELECT token, registered_block, from_block, to_block,
               SUM(CASE WHEN reach IS NULL OR from_block > reach + 1 THEN 1 ELSE 0 END) OVER (
                   PARTITION BY token ORDER BY from_block, to_block
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS island
        FROM edges
    ),
    merged AS (
        SELECT token, registered_block, island, MIN(from_block) AS from_block, MAX(to_block) AS to_block
        FROM islands GROUP BY token, registered_block, island
    )
    SELECT token, from_block, to_block, registered_block,
           registered_block IS NOT NULL
           AND from_block <= registered_block AND registered_block <= to_block AS from_creation
    FROM merged
    """,
    """
    CREATE TABLE IF NOT EXISTS tx_meta
    (
        txhash       TEXT PRIMARY KEY,
        block_number BIGINT,
        tx_index     INTEGER,
        from_addr    TEXT,
        to_addr      TEXT,
        value        NUMERIC(78, 0),
        selector     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tx_traces
    (
        txhash    TEXT PRIMARY KEY,
        available BOOLEAN NOT NULL,
        transfers JSONB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_meta
    (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
)


def missing_columns(cur, table: str, ddl: tuple[str, ...]) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    )
    present = {row[0] for row in cur.fetchall()}
    return [line for line in ddl if line.split()[0] not in present]


def init_ledger_schema(cur) -> None:
    """Create the ledger tables, and widen ones an earlier version created.

    The widening only runs when a column is actually absent, so a routine start takes no table lock at all.
    """
    for statement in _STATEMENTS:
        cur.execute(statement)
    for table, ddl in ADDED_COLUMNS:
        missing = missing_columns(cur, table, ddl)
        if missing:
            cur.execute(f"ALTER TABLE {table} " + ", ".join(f"ADD COLUMN IF NOT EXISTS {line}" for line in missing))
