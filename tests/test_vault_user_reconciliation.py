import asyncio
import os
import sys
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage.vaults as vault_storage
import core.stream as stream
import models
import state as state_module


@contextmanager
def _cursor(cur):
    yield cur


def test_set_vault_user_shares_updates_an_existing_position():
    cur = MagicMock()
    with patch.object(vault_storage, "db_cursor", side_effect=lambda: _cursor(cur)):
        vault_storage.set_crystal_vault_user_shares(
            vault="0xVAULT",
            user_address="0xUSER",
            shares=17,
        )

    sql, params = cur.execute.call_args.args
    assert "UPDATE crystal_vault_users" in sql
    assert "SET shares = GREATEST(%s, 0)" in sql
    assert params == (17, "0xvault", "0xuser")


def test_vault_user_share_reconciliation_replaces_stale_event_balance():
    state = state_module.State.__new__(state_module.State)
    state._lock = threading.RLock()
    state.vaultToUsers = {
        "0xvault": {
            "0xuser": models.VaultUser(
                address="0xuser",
                vault="0xvault",
                shares=2562,
                deposits=1,
                withdraws=0,
                lastDeposit=10,
                lastWithdraw=0,
            )
        }
    }

    with patch.object(state_module.storage, "set_crystal_vault_user_shares") as persist:
        assert state.vault_user_addresses() == [("0xvault", "0xuser")]
        state.reconcile_vault_user_shares("0xVAULT", "0xUSER", 0)
        assert state.vaultToUsers["0xvault"]["0xuser"].shares == 0
        persist.assert_called_once_with(vault="0xvault", user_address="0xuser", shares=0)
        state.reconcile_vault_user_shares("0xvault", "0xuser", 0)
        assert persist.call_count == 1


def test_vault_user_multicall_reconciles_contract_balances():
    vault = "0x" + "1" * 40
    user = "0x" + "2" * 40
    state = MagicMock()
    state.vault_user_addresses.return_value = [(vault, user)]
    rpc = AsyncMock(return_value={"result": "0xresult"})

    with (
        patch.object(stream.backfill, "http_jsonrpc", rpc),
        patch.object(stream, "_encode_multicall3_aggregate3", return_value="0xcall") as encode,
        patch.object(stream, "_decode_multicall3_aggregate3_result", return_value=[(True, (7).to_bytes(32, "big"))]),
    ):
        asyncio.run(stream._reconcile_vault_users_multicall(state, "0xabc"))

    calls = encode.call_args.args[0]
    assert calls == [(vault, stream.BALANCE_OF_SELECTOR + bytes.fromhex(user[2:].rjust(64, "0")))]
    state.reconcile_vault_user_shares.assert_called_once_with(vault, user, 7)
