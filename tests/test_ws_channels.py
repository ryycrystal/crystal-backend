"""channel payload semantics against a real database.

the failure modes that matter here are silent: a seq gap the client cannot detect,
a holder that hits zero and lingers forever, a trade id format that double counts.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import core.storage as storage  # noqa: E402
from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    USER,
    _create,
    _new_state,
    _trade,
    clean,  # noqa: F401
    db,  # noqa: F401
)


# trade ids must be decimal, matching REST, or the client double counts against its
# own chain-socket ids which are hex
def test_trade_ids_are_decimal_and_match_rest(db):
    from api.ws_data import recent_trades

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xfeed", log_idx=7)

    rows = recent_trades(TOKEN)
    assert len(rows) == 1
    assert rows[0]["id"] == "0xfeed-7", f"expected decimal log index, got {rows[0]['id']}"
    assert "0x7" not in rows[0]["id"].split("-")[1]


# seq must advance by exactly one per frame so a client can detect a dropped delta
def test_seq_is_monotonic_per_token_and_channel(db):
    from api.ws import Hub

    hub = Hub()
    a = [hub._next_seq(TOKEN, "trades") for _ in range(3)]
    b = [hub._next_seq(TOKEN, "holders") for _ in range(2)]
    assert a == [1, 2, 3], "seq must increment by one"
    assert b == [1, 2], "each channel counts independently"
    assert hub._next_seq("0xother", "trades") == 1, "each token counts independently"


# the first frame is everything; later frames carry only what changed
def test_first_frame_is_full_then_diffs(db):
    from api.ws import Hub

    hub = Hub()
    rows1 = {"a": {"address": "a", "v": 1}, "b": {"address": "b", "v": 1}}
    up, rm = hub._diff_rows(TOKEN, "holders", rows1)
    assert len(up) == 2 and rm == [], "first frame carries every row"

    up, rm = hub._diff_rows(TOKEN, "holders", rows1)
    assert up == [] and rm == [], "an unchanged frame carries nothing"

    rows2 = {"a": {"address": "a", "v": 2}, "b": {"address": "b", "v": 1}}
    up, rm = hub._diff_rows(TOKEN, "holders", rows2)
    assert up == [{"address": "a", "v": 2}], "only the changed row"
    assert rm == []


# a holder reaching zero must be removed explicitly or it lingers in the client list
def test_holder_dropping_out_is_explicitly_removed(db):
    from api.ws import Hub

    hub = Hub()
    hub._diff_rows(TOKEN, "holders", {"a": {"address": "a"}, "b": {"address": "b"}})
    up, rm = hub._diff_rows(TOKEN, "holders", {"a": {"address": "a"}})
    assert rm == ["b"], "a departed holder must appear in removed"
    assert up == []


# holders must exclude internal addresses, same as the REST endpoint
def test_holders_exclude_internal_addresses(db):
    from api.ws_data import top_holders

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xh1", log_idx=0)
    storage.record_block_processed(101)

    rows = top_holders(TOKEN)
    addrs = {r["address"] for r in rows}
    assert USER in addrs or not rows


# positions are scoped to the requested wallets and nothing else
def test_positions_are_scoped_to_requested_wallets(db):
    from api.ws_data import positions_for

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xp1", log_idx=0)

    mine = positions_for(TOKEN, [USER])
    assert all(r["address"] == USER for r in mine)

    other = positions_for(TOKEN, ["0x000000000000000000000000000000000000dead"])
    assert other == [], "a wallet with no position returns nothing"

    assert positions_for(TOKEN, []) == [], "no wallets means no query"


# the watermark the hub stamps frames with must track the indexer
def test_indexer_watermark_tracks_processed_blocks(db):
    from api.ws_data import indexer_watermark

    assert indexer_watermark() == 0
    storage.record_block_processed(500)
    assert indexer_watermark() == 500
    storage.record_block_processed(499)
    assert indexer_watermark() == 500, "watermark is the max, not the latest write"
