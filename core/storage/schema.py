from __future__ import annotations

from .base import db_cursor

def init_db() -> None:
    with db_cursor() as cur:

        cur.execute(
            """
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            """
        )
        

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
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_token
            ON launchpad_trades (user_address, token);
            """
        )
        

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
                approaching_75_at    BIGINT,
                quote_token          TEXT NOT NULL DEFAULT '0x3bd359c1119da7da1d913d1c4d2b7c461115433a'
            ); 
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_tokens
            ADD COLUMN IF NOT EXISTS quote_token TEXT NOT NULL DEFAULT '0x3bd359c1119da7da1d913d1c4d2b7c461115433a';
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
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_approaching
            ON launchpad_tokens (circulating_supply DESC)
            WHERE approaching_75 = TRUE AND migrated = FALSE;
            """
        )
        

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
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_history_keyset
            ON launchpad_trades (user_address, timestamp DESC, log_index DESC, txhash DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_user_token_history_keyset
            ON launchpad_trades (user_address, token, timestamp DESC, log_index DESC, txhash DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_user_pnl_keyset
            ON launchpad_positions (user_address, total_pnl_native DESC, token DESC)
            WHERE balance_token > 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_user_balance_keyset
            ON launchpad_positions (user_address, balance_token DESC, token DESC)
            WHERE balance_token > 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_users_pnl_leaderboard
            ON launchpad_users (total_realized_pnl_native DESC)
            WHERE total_realized_pnl_native > 0;
            """
        )


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
        

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_meta
            (
                key   TEXT PRIMARY KEY,
                value NUMERIC(50, 18) NOT NULL
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_markets
            (
                market         TEXT PRIMARY KEY,
                is_canonical   BOOLEAN NOT NULL,
                quote_asset    TEXT NOT NULL,
                base_asset     TEXT NOT NULL,
                quote_address  TEXT NOT NULL,
                quote_decimals INTEGER NOT NULL,
                quote_ticker   TEXT NOT NULL,
                quote_name     TEXT NOT NULL,
                base_address   TEXT NOT NULL,
                base_decimals  INTEGER NOT NULL,
                base_ticker    TEXT NOT NULL,
                base_name      TEXT NOT NULL,
                market_id      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                market_type    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                scale_factor   NUMERIC(78, 0) NOT NULL DEFAULT 0,
                tick_size      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                max_price      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                min_size       NUMERIC(78, 0) NOT NULL DEFAULT 0,
                taker_fee      NUMERIC(78, 0) NOT NULL DEFAULT 0,
                maker_rebate   NUMERIC(78, 0) NOT NULL DEFAULT 0,
                is_amm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                last_price     NUMERIC(50, 18) NOT NULL DEFAULT 0,
                created_block  BIGINT,
                created_at     BIGINT,
                updated_block  BIGINT,
                updated_at     BIGINT
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_markets_quote_base
            ON crystal_markets (quote_address, base_address);
            """
        )
        cur.execute(
            """
            ALTER TABLE crystal_markets
            ADD COLUMN IF NOT EXISTS is_amm_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            """
        )
        cur.execute(
            """
            UPDATE crystal_markets
            SET is_amm_enabled = (market_type > 1)
            WHERE is_amm_enabled IS DISTINCT FROM (market_type > 1);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_pools
            (
                market          TEXT PRIMARY KEY,
                reserve_quote   NUMERIC(78, 0) NOT NULL DEFAULT 0,
                reserve_base    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                total_shares    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                tvl_usd         NUMERIC(50, 18) NOT NULL DEFAULT 0,
                volume_24h_usd  NUMERIC(50, 18) NOT NULL DEFAULT 0,
                fees_24h_usd    NUMERIC(50, 18) NOT NULL DEFAULT 0,
                apy_24h         NUMERIC(50, 18) NOT NULL DEFAULT 0,
                daily_yield_24h NUMERIC(50, 18) NOT NULL DEFAULT 0,
                last_sync_block BIGINT,
                last_sync_at    BIGINT,
                updated_block   BIGINT,
                updated_at      BIGINT
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_pools_updated
            ON crystal_pools (COALESCE(updated_at, last_sync_at, 0) DESC, market ASC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_pools_tvl
            ON crystal_pools (tvl_usd DESC, market ASC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_pool_sync_events
            (
                id                 BIGSERIAL PRIMARY KEY,
                block_number       BIGINT NOT NULL,
                log_index          INTEGER NOT NULL,
                timestamp          BIGINT NOT NULL,
                market             TEXT NOT NULL,
                txhash             TEXT NOT NULL,
                kind               TEXT NOT NULL,
                reserve_quote      NUMERIC(78, 0) NOT NULL,
                reserve_base       NUMERIC(78, 0) NOT NULL,
                prev_reserve_quote NUMERIC(78, 0),
                prev_reserve_base  NUMERIC(78, 0),
                volume_usd         NUMERIC(50, 18) NOT NULL DEFAULT 0,
                fees_usd           NUMERIC(50, 18) NOT NULL DEFAULT 0,
                UNIQUE (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_pool_sync_events_market_ts
            ON crystal_pool_sync_events (market, timestamp DESC, log_index DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_pool_sync_events_market_kind_ts
            ON crystal_pool_sync_events (market, kind, timestamp DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_pool_tvl_samples
            (
                market        TEXT NOT NULL,
                block_number  BIGINT NOT NULL,
                log_index     INTEGER NOT NULL,
                timestamp     BIGINT NOT NULL,
                reserve_quote NUMERIC(78, 0) NOT NULL,
                reserve_base  NUMERIC(78, 0) NOT NULL,
                tvl_usd       NUMERIC(50, 18) NOT NULL DEFAULT 0,
                txhash        TEXT NOT NULL,
                PRIMARY KEY (market, block_number, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_pool_tvl_samples_market_ts
            ON crystal_pool_tvl_samples (market, timestamp DESC, block_number DESC, log_index DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_pool_lp_users
            (
                market        TEXT NOT NULL,
                user_address  TEXT NOT NULL,
                shares        NUMERIC(78, 0) NOT NULL DEFAULT 0,
                last_transfer BIGINT NOT NULL DEFAULT 0,
                updated_block BIGINT,
                PRIMARY KEY (market, user_address)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_pool_lp_users_market_shares
            ON crystal_pool_lp_users (market, shares DESC, user_address ASC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vaults
            (
                vault                 TEXT PRIMARY KEY,
                quote                 TEXT NOT NULL,
                base                  TEXT NOT NULL,
                market                TEXT NOT NULL DEFAULT '',
                owner                 TEXT NOT NULL,
                name                  TEXT NOT NULL DEFAULT '',
                description           TEXT NOT NULL DEFAULT '',
                social1               TEXT NOT NULL DEFAULT '',
                social2               TEXT NOT NULL DEFAULT '',
                social3               TEXT NOT NULL DEFAULT '',
                locked                BOOLEAN NOT NULL DEFAULT FALSE,
                closed                BOOLEAN NOT NULL DEFAULT FALSE,
                max_shares            NUMERIC(78, 0) NOT NULL DEFAULT 0,
                circulating_shares    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                quote_decimals        INTEGER NOT NULL DEFAULT 0,
                base_decimals         INTEGER NOT NULL DEFAULT 0,
                lockup                NUMERIC(78, 0) NOT NULL DEFAULT 0,
                decrease_on_withdraw  BOOLEAN NOT NULL DEFAULT FALSE,
                deployed_block        BIGINT,
                deployed_at           BIGINT,
                updated_block         BIGINT,
                updated_at            BIGINT
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vaults_quote_base
            ON crystal_vaults (quote, base);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_users
            (
                vault         TEXT NOT NULL,
                user_address  TEXT NOT NULL,
                shares        NUMERIC(78, 0) NOT NULL DEFAULT 0,
                deposits      BIGINT NOT NULL DEFAULT 0,
                withdraws     BIGINT NOT NULL DEFAULT 0,
                last_deposit  BIGINT NOT NULL DEFAULT 0,
                last_withdraw BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (vault, user_address)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_vault_shares
            ON crystal_vault_users (vault, shares DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_vault_lastdep
            ON crystal_vault_users (vault, last_deposit DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_vault_lastdep_shares_addr
            ON crystal_vault_users (vault, last_deposit DESC, shares DESC, user_address ASC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_deposits
            (
                id           BIGSERIAL PRIMARY KEY,
                block_number BIGINT NOT NULL,
                log_index    INTEGER NOT NULL,
                timestamp    BIGINT NOT NULL,
                vault        TEXT NOT NULL,
                user_address TEXT NOT NULL,
                shares       NUMERIC(78, 0) NOT NULL,
                quote_amount NUMERIC(78, 0) NOT NULL,
                base_amount  NUMERIC(78, 0) NOT NULL,
                txhash       TEXT NOT NULL,
                UNIQUE (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_deposits_vault_ts
            ON crystal_vault_deposits (vault, timestamp DESC, log_index DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_withdrawals
            (
                id           BIGSERIAL PRIMARY KEY,
                block_number BIGINT NOT NULL,
                log_index    INTEGER NOT NULL,
                timestamp    BIGINT NOT NULL,
                vault        TEXT NOT NULL,
                user_address TEXT NOT NULL,
                shares       NUMERIC(78, 0) NOT NULL,
                quote_amount NUMERIC(78, 0) NOT NULL,
                base_amount  NUMERIC(78, 0) NOT NULL,
                txhash       TEXT NOT NULL,
                UNIQUE (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_withdrawals_vault_ts
            ON crystal_vault_withdrawals (vault, timestamp DESC, log_index DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_vault_balance_samples
            (
                vault         TEXT NOT NULL,
                block_number  BIGINT NOT NULL,
                timestamp     BIGINT NOT NULL,
                quote_balance NUMERIC(78, 0) NOT NULL,
                base_balance  NUMERIC(78, 0) NOT NULL,
                usd_value     NUMERIC(50, 18) NOT NULL,
                PRIMARY KEY (vault, block_number)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_balances_vault_ts
            ON crystal_vault_balance_samples (vault, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_balances_vault_ts_block
            ON crystal_vault_balance_samples (vault, timestamp DESC, block_number DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS holder_denylist
            (
                address  TEXT PRIMARY KEY,
                label    TEXT NOT NULL DEFAULT '',
                note     TEXT NOT NULL DEFAULT '',
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nadfun_v2_tokens
            (
                token TEXT PRIMARY KEY
            );
            """
        )


