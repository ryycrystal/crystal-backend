from __future__ import annotations

LEDGER_TABLES = (
    "wallet_flows",
    "positions_v2",
    "address_kinds",
    "venues",
    "token_registry",
    "tx_meta",
    "tx_traces",
    "ledger_meta",
)

_STATEMENTS = (
    """
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
        realized_delta NUMERIC(78, 0) NOT NULL DEFAULT 0,
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
    """
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
        flow_count                 INTEGER,
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


def init_ledger_schema(cur) -> None:
    for statement in _STATEMENTS:
        cur.execute(statement)
