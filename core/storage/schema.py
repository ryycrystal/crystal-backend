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
            CREATE INDEX IF NOT EXISTS idx_trades_ts
            ON launchpad_trades (timestamp DESC);
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
            ALTER TABLE launchpad_blocks
            ADD COLUMN IF NOT EXISTS block_hash TEXT;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_trades
            ADD COLUMN IF NOT EXISTS native_reserve NUMERIC(78, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_trades
            ADD COLUMN IF NOT EXISTS token_reserve NUMERIC(78, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_trades
            ADD COLUMN IF NOT EXISTS realized_native NUMERIC(50, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_launchpad_trades_block
            ON launchpad_trades (block_number);
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_tokens
            ADD COLUMN IF NOT EXISTS curve_native_reserve NUMERIC(78, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_tokens
            ADD COLUMN IF NOT EXISTS ath_price_native NUMERIC(50, 18) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            UPDATE launchpad_tokens t
            SET ath_price_native = s.mx
            FROM (
                SELECT token, MAX(price_native) AS mx
                FROM launchpad_trades
                GROUP BY token
            ) s
            WHERE t.token = s.token AND t.ath_price_native < s.mx;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_tokens
            ADD COLUMN IF NOT EXISTS curve_token_reserve NUMERIC(78, 0) NOT NULL DEFAULT 0;
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
            CREATE INDEX IF NOT EXISTS idx_tokens_created_order
            ON launchpad_tokens (created_at DESC NULLS LAST, created_block DESC NULLS LAST);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_migrated_order
            ON launchpad_tokens (migrated, migrated_at DESC NULLS LAST, migrated_block DESC NULLS LAST);
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
            CREATE OR REPLACE FUNCTION crystal_unrealized_pnl(
                hold NUMERIC, bought NUMERIC, sold NUMERIC, basis NUMERIC, price NUMERIC
            ) RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS $fn$
                SELECT GREATEST(hold, 0) * COALESCE(price, 0)
                     - CASE WHEN GREATEST(bought - sold, 0) > 0
                            THEN ROUND(
                                GREATEST(basis, 0)
                                * LEAST(GREATEST(hold, 0), GREATEST(bought - sold, 0))
                                / GREATEST(bought - sold, 0))
                            ELSE 0 END
            $fn$;
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
            ALTER TABLE launchpad_positions
            ADD COLUMN IF NOT EXISTS cost_basis_native NUMERIC(78, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE VIEW launchpad_positions_live AS
            SELECT p.user_address, p.token, p.token_bought, p.token_sold,
                   p.native_spent, p.native_received, p.balance_token,
                   p.realized_pnl_native,
                   crystal_unrealized_pnl(p.balance_token, p.token_bought, p.token_sold,
                                          p.cost_basis_native, k.last_price_native)
                       AS unrealized_pnl_native,
                   p.realized_pnl_native
                       + crystal_unrealized_pnl(p.balance_token, p.token_bought, p.token_sold,
                                                p.cost_basis_native, k.last_price_native)
                       AS total_pnl_native,
                   p.trade_count, p.buy_count, p.sell_count, p.cost_basis_native
            FROM launchpad_positions p
            LEFT JOIN launchpad_tokens k ON k.token = p.token;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_tokens
            ADD COLUMN IF NOT EXISTS token_uri TEXT;
            """
        )
        cur.execute(
            """
            ALTER TABLE launchpad_tokens
            ADD COLUMN IF NOT EXISTS metadata_attempts INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS metadata_tried_at BIGINT NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_missing_metadata
            ON launchpad_tokens (token)
            WHERE (metadata_cid IS NULL OR metadata_cid = '') AND token_uri IS NOT NULL;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tokens_metadata_retry
            ON launchpad_tokens (metadata_tried_at)
            WHERE (metadata_cid IS NULL OR metadata_cid = '') AND token_uri IS NOT NULL;
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
        for _col in (
            "reserve_token NUMERIC(78, 0) NOT NULL DEFAULT 0",
            "reserve_native NUMERIC(78, 0) NOT NULL DEFAULT 0",
            "last_sync_block BIGINT",
            "last_sync_at BIGINT",
        ):
            cur.execute(f"ALTER TABLE launchpad_pools ADD COLUMN IF NOT EXISTS {_col};")

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
            CREATE TABLE IF NOT EXISTS crystal_pool_liquidity_events
            (
                txhash        TEXT NOT NULL,
                log_index     BIGINT NOT NULL,
                market        TEXT NOT NULL,
                kind          TEXT NOT NULL,
                user_address  TEXT DEFAULT '',
                amount_quote  NUMERIC(78, 0) NOT NULL DEFAULT 0,
                amount_base   NUMERIC(78, 0) NOT NULL DEFAULT 0,
                shares        NUMERIC(78, 0),
                block_number  BIGINT NOT NULL DEFAULT 0,
                timestamp     BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pool_liq_market_ts
            ON crystal_pool_liquidity_events (market, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pool_liq_user_ts
            ON crystal_pool_liquidity_events (user_address, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_pool_lp_users
            (
                market        TEXT NOT NULL,
                user_address  TEXT NOT NULL,
                shares        NUMERIC(78, 0) NOT NULL DEFAULT 0,
                cost_quote    NUMERIC(78, 0) NOT NULL DEFAULT 0,
                cost_base     NUMERIC(78, 0) NOT NULL DEFAULT 0,
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
            CREATE INDEX IF NOT EXISTS idx_crystal_pool_lp_users_user
            ON crystal_pool_lp_users (user_address);
            """
        )
        _migrate_lp_user_cost_columns(cur)

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
            CREATE INDEX IF NOT EXISTS idx_crystal_vault_users_user
            ON crystal_vault_users (user_address);
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
            ALTER TABLE crystal_vault_balance_samples
            ADD COLUMN IF NOT EXISTS shares NUMERIC(78, 0);
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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_kv
            (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_bindings
            (
                referee      TEXT PRIMARY KEY,
                referrer     TEXT NOT NULL,
                block_number BIGINT NOT NULL,
                log_index    INTEGER NOT NULL,
                timestamp    BIGINT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_referral_bindings_referrer
            ON referral_bindings (referrer);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_rewards
            (
                referrer   TEXT NOT NULL,
                token      TEXT NOT NULL,
                claimable  NUMERIC(78, 0) NOT NULL DEFAULT 0,
                earned     NUMERIC(78, 0) NOT NULL DEFAULT 0,
                updated_at BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (referrer, token)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_claims
            (
                txhash       TEXT NOT NULL,
                log_index    INTEGER NOT NULL,
                claim_index  INTEGER NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp    BIGINT NOT NULL,
                user_address TEXT NOT NULL,
                token        TEXT NOT NULL,
                amount       NUMERIC(78, 0) NOT NULL,
                PRIMARY KEY (txhash, log_index, claim_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_referral_claims_user_ts
            ON referral_claims (user_address, timestamp DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS volume_tiers
            (
                tier                    INTEGER PRIMARY KEY,
                name                    TEXT NOT NULL,
                min_volume_usd          NUMERIC(50, 2) NOT NULL,
                cashback_multiplier     NUMERIC(10, 2) NOT NULL DEFAULT 1,
                referral_commission_bps INTEGER NOT NULL DEFAULT 1000
            );
            """
        )
        cur.execute(
            """
            INSERT INTO volume_tiers (tier, name, min_volume_usd, cashback_multiplier, referral_commission_bps)
            VALUES (0, 'Basic', 0, 1, 1000),
                   (1, 'Bronze', 10000, 1, 1000),
                   (2, 'Silver', 100000, 2, 2000),
                   (3, 'Gold', 1000000, 5, 3000),
                   (4, 'Diamond', 10000000, 10, 4000)
            ON CONFLICT (tier) DO NOTHING;
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_orderbook_events
            (
                txhash       TEXT NOT NULL,
                log_index    INTEGER NOT NULL,
                entry_index  INTEGER NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp    BIGINT NOT NULL,
                market       TEXT NOT NULL,
                user_address TEXT NOT NULL,
                flag         SMALLINT NOT NULL,
                is_buy       BOOLEAN NOT NULL,
                action       TEXT NOT NULL,
                price        NUMERIC(30, 0) NOT NULL,
                order_id     BIGINT NOT NULL,
                size         NUMERIC(40, 0) NOT NULL,
                PRIMARY KEY (txhash, log_index, entry_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ob_events_user_ts
            ON crystal_orderbook_events (user_address, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ob_events_market_ts
            ON crystal_orderbook_events (market, timestamp DESC);
            """
        )

        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.key_column_usage
            WHERE table_name = 'crystal_orderbook_orders'
              AND constraint_name = 'crystal_orderbook_orders_pkey'
            """
        )
        if int(cur.fetchone()[0] or 0) == 2:
            cur.execute("DROP TABLE crystal_orderbook_orders")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_orderbook_orders
            (
                market        TEXT NOT NULL,
                price         NUMERIC(30, 0) NOT NULL,
                order_id      BIGINT NOT NULL,
                user_address  TEXT NOT NULL,
                is_buy        BOOLEAN NOT NULL,
                size          NUMERIC(40, 0) NOT NULL,
                original_size NUMERIC(40, 0) NOT NULL DEFAULT 0,
                filled_size   NUMERIC(40, 0) NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'open',
                created_block BIGINT NOT NULL DEFAULT 0,
                created_ts    BIGINT NOT NULL DEFAULT 0,
                updated_block BIGINT NOT NULL DEFAULT 0,
                updated_ts    BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (market, price, order_id)
            );
            """
        )
        cur.execute(
            """
            ALTER TABLE crystal_orderbook_orders
            ADD COLUMN IF NOT EXISTS original_size NUMERIC(40, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            ALTER TABLE crystal_orderbook_orders
            ADD COLUMN IF NOT EXISTS filled_size NUMERIC(40, 0) NOT NULL DEFAULT 0;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ob_events_order
            ON crystal_orderbook_events (market, price, order_id);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ob_orders_user_status
            ON crystal_orderbook_orders (user_address, status);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ob_orders_user_updated
            ON crystal_orderbook_orders (user_address, updated_ts DESC, price DESC, order_id DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_wallet_prefs
            (
                key          TEXT PRIMARY KEY,
                wallet_count INTEGER NOT NULL DEFAULT 0,
                selected     JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at   BIGINT NOT NULL DEFAULT 0
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_users
            (
                user_id      BIGINT PRIMARY KEY,
                user_address TEXT NOT NULL,
                is_margin    BOOLEAN NOT NULL DEFAULT FALSE,
                block_number BIGINT NOT NULL DEFAULT 0,
                timestamp    BIGINT NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crystal_users_addr ON crystal_users (user_address);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_orderbook_fills
            (
                txhash       TEXT NOT NULL,
                log_index    INTEGER NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp    BIGINT NOT NULL,
                market       TEXT NOT NULL,
                maker        TEXT NOT NULL,
                maker_is_buy BOOLEAN NOT NULL,
                price        NUMERIC(30, 0) NOT NULL,
                order_id     BIGINT NOT NULL,
                remaining    NUMERIC(40, 0) NOT NULL,
                amount_high  NUMERIC(40, 0) NOT NULL,
                amount_out   NUMERIC(40, 0) NOT NULL,
                PRIMARY KEY (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ob_fills_maker_ts
            ON crystal_orderbook_fills (maker, timestamp DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS crystal_market_trades
            (
                txhash       TEXT NOT NULL,
                log_index    INTEGER NOT NULL,
                block_number BIGINT NOT NULL,
                timestamp    BIGINT NOT NULL,
                market       TEXT NOT NULL,
                user_address TEXT NOT NULL,
                is_buy       BOOLEAN NOT NULL,
                amount_in    NUMERIC(50, 0) NOT NULL,
                amount_out   NUMERIC(50, 0) NOT NULL,
                start_price  NUMERIC(30, 0) NOT NULL,
                end_price    NUMERIC(30, 0) NOT NULL,
                PRIMARY KEY (txhash, log_index)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mkt_trades_user_ts
            ON crystal_market_trades (user_address, timestamp DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mkt_trades_market_ts
            ON crystal_market_trades (market, timestamp DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS spot_graph_buckets
            (
                wallet       TEXT NOT NULL,
                bucket_ts    BIGINT NOT NULL,
                block_number BIGINT NOT NULL,
                value_usd    NUMERIC(50, 18) NOT NULL,
                value_native NUMERIC(50, 18) NOT NULL,
                balances     JSONB NOT NULL,
                PRIMARY KEY (wallet, bucket_ts)
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS launchpad_pair_fees
            (
                pair TEXT PRIMARY KEY,
                ok BOOLEAN NOT NULL DEFAULT FALSE,
                fee_collector TEXT DEFAULT '',
                base_token TEXT DEFAULT '',
                quote_token TEXT DEFAULT '',
                creator_fee_rate NUMERIC DEFAULT 0,
                curve_protocol_fee_rate NUMERIC DEFAULT 0,
                dex_protocol_fee_rate NUMERIC DEFAULT 0,
                fetched_at BIGINT DEFAULT 0
            );
            """
        )

        cur.execute(
            """
            ALTER TABLE launchpad_pair_fees
            ADD COLUMN IF NOT EXISTS pool_fee_ppm INTEGER NOT NULL DEFAULT 0;
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS x_tracked_users
            (
                username TEXT PRIMARY KEY,
                added_at TIMESTAMPTZ NOT NULL DEFAULT Now()
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS x_tweets
            (
                id          BIGSERIAL PRIMARY KEY,
                tweet_id    TEXT NOT NULL UNIQUE,
                username    TEXT NOT NULL,
                created_at  BIGINT NOT NULL DEFAULT 0,
                payload     JSONB NOT NULL,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT Now()
            );
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_x_tweets_user_created
            ON x_tweets (username, created_at DESC, id DESC);
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS x_poll_leader
            (
                id           INTEGER PRIMARY KEY,
                holder       TEXT,
                heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT Now()
            );
            """
        )

        cur.execute(
            """
            INSERT INTO x_poll_leader (id, holder, heartbeat_at)
            VALUES (1, NULL, Now() - INTERVAL '1 hour')
            ON CONFLICT (id) DO NOTHING;
            """
        )

        cur.execute(
            """
            UPDATE launchpad_tokens t
            SET source = 2
            WHERE t.source = 1
              AND EXISTS (SELECT 1 FROM nadfun_v2_tokens v WHERE v.token = t.token);
            """
        )


def backfill_cost_basis() -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM launchpad_positions
            WHERE cost_basis_native = 0 AND native_spent > 0
              AND ROUND(GREATEST(native_spent - CASE WHEN token_bought > 0 THEN native_spent * LEAST(token_sold, token_bought) / token_bought ELSE 0 END, 0)) > 0
            LIMIT 1;
            """
        )
        if cur.fetchone() is None:
            return

    while True:
        with db_cursor() as cur:
            cur.execute(
                """
                WITH batch AS (
                    SELECT user_address, token
                    FROM launchpad_positions
                    WHERE cost_basis_native = 0 AND native_spent > 0
                      AND ROUND(GREATEST(native_spent - CASE WHEN token_bought > 0 THEN native_spent * LEAST(token_sold, token_bought) / token_bought ELSE 0 END, 0)) > 0
                    LIMIT 5000
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE launchpad_positions p
                SET cost_basis_native = GREATEST(
                        p.native_spent
                        - CASE WHEN p.token_bought > 0
                               THEN p.native_spent * LEAST(p.token_sold, p.token_bought) / p.token_bought
                               ELSE 0 END,
                        0)
                FROM batch b
                WHERE p.user_address = b.user_address AND p.token = b.token;
                """
            )
            done = cur.rowcount or 0
        if done <= 0:
            return
        print(f"[DB] cost basis backfill: {done} positions", flush=True)


def _migrate_lp_user_cost_columns(cur) -> None:
    cur.execute(
        "ALTER TABLE crystal_pool_lp_users ADD COLUMN IF NOT EXISTS cost_quote NUMERIC(78,0) NOT NULL DEFAULT 0;"
    )
    cur.execute(
        "ALTER TABLE crystal_pool_lp_users ADD COLUMN IF NOT EXISTS cost_base NUMERIC(78,0) NOT NULL DEFAULT 0;"
    )


def backfill_pool_fees_k_growth() -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE crystal_pool_sync_events e
            SET fees_usd = GREATEST(
                COALESCE(t.tvl_usd, 0) * (SQRT(
                    (e.reserve_quote * e.reserve_base)::numeric
                    / NULLIF((e.prev_reserve_quote * e.prev_reserve_base)::numeric, 0)
                ) - 1), 0)
            FROM crystal_pool_tvl_samples t
            WHERE e.kind = 'trade'
              AND e.prev_reserve_quote > 0 AND e.prev_reserve_base > 0
              AND e.reserve_quote > 0 AND e.reserve_base > 0
              AND t.market = e.market AND t.block_number = e.block_number
              AND t.log_index = e.log_index;
            """
        )
        n = cur.rowcount or 0
    if n:
        print(f"[DB] pool fee k-growth backfill: {n} trade syncs recomputed", flush=True)
