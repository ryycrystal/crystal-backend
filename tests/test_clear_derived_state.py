import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage.launchpad as lp

UNREBUILDABLE_TABLES = (
    "crystal_vault_balance_samples",
    "referral_bindings",
    "referral_rewards",
    "launchpad_block_logs",
    "launchpad_kv",
)

DERIVED_TABLES = (
    "launchpad_trades",
    "launchpad_ohlcv",
    "launchpad_positions",
    "launchpad_users",
    "launchpad_tokens",
    "launchpad_blocks",
    "crystal_vaults",
    "crystal_vault_users",
    "crystal_markets",
    "crystal_pools",
    "crystal_orderbook_events",
    "crystal_orderbook_orders",
    "crystal_orderbook_fills",
    "crystal_market_trades",
)


def _executed_sql() -> str:
    cur = MagicMock()
    lp._clear_derived_state_impl(0, cur)
    return " ".join(call[0][0] for call in cur.execute.call_args_list)


def test_clear_derived_state_never_touches_unrebuildable_tables():
    sql = _executed_sql()
    for table in UNREBUILDABLE_TABLES:
        assert table not in sql, (
            f"{table} is not rebuildable from cached logs, so a clean reindex that deletes it "
            "destroys history permanently"
        )


def test_clear_derived_state_wipes_every_log_derived_table():
    sql = _executed_sql()
    for table in DERIVED_TABLES:
        assert f"DELETE FROM {table}" in sql
