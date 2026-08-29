import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill
import core.storage.launchpad as lp


def _run_reindex(last_db: int, resume: bool):
    with patch.object(backfill.storage, "get_cached_block_range", return_value=(100, 200)):
        with patch.object(backfill.storage, "get_last_processed_block", return_value=last_db):
            with patch.object(backfill.storage, "count_uncached_processed_blocks", return_value=0):
                with patch.object(backfill.storage, "clear_derived_state_from_block") as wipe:
                    with patch.object(backfill.storage, "db_cursor"):
                        with patch.object(backfill.storage, "get_block_logs_range", return_value={}):
                            with patch.object(backfill.SEQUENCER, "process_chunk"):
                                with patch.object(backfill.SEQUENCER, "reset_pending"):
                                    with patch.object(backfill.SEQUENCER._state, "rebuild_from_db") as rebuilt:
                                        with patch.object(backfill.SEQUENCER._state, "reset_for_reindex"):
                                            asyncio.run(backfill.reindex(150, 50, resume=resume))
    return wipe, rebuilt


def test_resume_never_wipes_even_when_nothing_is_processed_yet():
    wipe, rebuilt = _run_reindex(last_db=0, resume=True)
    wipe.assert_not_called()
    rebuilt.assert_called_once()


def test_resume_behind_the_start_block_still_never_wipes():
    wipe, _ = _run_reindex(last_db=120, resume=True)
    wipe.assert_not_called()


def test_resume_from_a_populated_db_never_wipes():
    wipe, rebuilt = _run_reindex(last_db=180, resume=True)
    wipe.assert_not_called()
    rebuilt.assert_called_once()


def test_only_an_explicit_clean_wipes():
    wipe, _ = _run_reindex(last_db=0, resume=False)
    wipe.assert_called_once()


def test_generation_purge_scopes_deletes_to_crystal_rows():
    cur = MagicMock()
    cur.rowcount = 3
    removed = lp.delete_crystal_generation_before(100_095_258, cur)

    statements = [call[0][0] for call in cur.execute.call_args_list]
    joined = " ".join(statements)

    assert "DELETE FROM launchpad_tokens WHERE token IN" in joined
    assert "source = %(src)s" in joined
    assert "DELETE FROM launchpad_tokens" in joined
    assert " DELETE FROM launchpad_ohlcv WHERE token IN" in " " + joined
    assert "launchpad_block_logs" not in joined, "the raw log cache must survive a generation purge"
    assert "referral" not in joined, "referral data is not generation scoped"
    assert "crystal_vault" not in joined, "vaults are decided separately, not swept by generation"
    assert set(removed) >= {"launchpad_trades", "crystal_markets", "launchpad_tokens"}


def test_purge_params_carry_the_crystal_source_and_block():
    cur = MagicMock()
    cur.rowcount = 0
    lp.delete_crystal_generation_before(12345, cur)
    params = cur.execute.call_args_list[0][0][1]
    assert params == {"src": lp.CRYSTAL_LAUNCHPAD_SOURCE, "blk": 12345}
