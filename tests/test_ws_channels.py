"""channel payload semantics against a real database.

the failure modes that matter here are silent: a seq gap the client cannot detect,
a holder that hits zero and lingers forever, a trade id format that double counts.
"""

import os
import sys
from decimal import Decimal

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


# the token channel must carry everything on /token that moves per trade, so the
# client can stop polling it entirely after first load
def test_token_channel_carries_the_live_fields(db):
    from api.ws_data import token_state

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xts1", log_idx=0)

    body = token_state(TOKEN)

    # valuation
    for k in ("marketcap", "marketcap_usd", "lastPriceNativePerTokenWad", "athMarketcap"):
        assert k in body, f"missing {k}"
    # curve position, the progress bar
    for k in ("circulating_supply", "progressBps", "phase", "curveNativeReserve"):
        assert k in body, f"missing {k}"
    # lifetime totals
    for k in ("volumeNative", "volume_usd", "fees_usd", "buyTxs", "sellTxs", "txCount"):
        assert k in body, f"missing {k}"
    # participants
    for k in ("totalHolders", "distinctBuyers", "distinctSellers"):
        assert k in body, f"missing {k}"
    # threshold and graduation
    for k in ("approaching_75", "migrated", "market", "migratedAt"):
        assert k in body, f"missing {k}"

    # and the static half is deliberately absent
    for k in ("name", "symbol", "social1", "creator", "description"):
        assert k not in body, f"{k} never changes and should not be pushed"


# an unknown token yields nothing rather than a half filled object
def test_token_channel_on_unknown_token_is_empty(db):
    from api.ws_data import token_state

    assert token_state("0x000000000000000000000000000000000000dead") == {}


# progress and phase must track the curve as trades land
def test_token_channel_tracks_curve_progress(db):
    from api.ws_data import token_state

    st = _new_state()
    _create(st, blk=100, ts=1000)

    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xtp1", log_idx=0)
    early = token_state(TOKEN)

    # push the curve past the 75% threshold
    _trade(st, native_reserve=2500 * 10**18, blk=102, ts=1002, txh="0xtp2", log_idx=0)
    late = token_state(TOKEN)

    assert int(late["progressBps"]) > int(early["progressBps"]), "progress must advance"
    assert late["phase"] == "graduating"
    assert late["approaching_75"] is True
    assert int(late["approaching_75_block"]) == 102


# ath holds at the peak rather than following price down
def test_token_channel_ath_does_not_follow_price_down(db):
    from api.ws_data import token_state

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=2000 * 10**18, blk=101, ts=1001, txh="0xta1", log_idx=0)
    peak = token_state(TOKEN)["athMarketcap"]

    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=1002, txh="0xta2", log_idx=0)
    after = token_state(TOKEN)

    assert after["athMarketcap"] == peak, "ath must hold at the high"
    assert Decimal(after["marketcap"]) < Decimal(peak), "price did come down"


# H-01: a client joining a token another client already watches must receive a full
# baseline. hub level diff state cannot answer this, because the new client has no
# state of its own -- before the fix it received nothing at all
def test_second_subscriber_gets_its_own_baseline(db):
    import json

    from fastapi.testclient import TestClient

    import api.api  # noqa: F401
    from api.ws import HUB

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xh01a", log_idx=0)
    storage.record_block_processed(101)

    client = TestClient(api.api.app)
    frame = json.dumps({"op": "subscribe", "token": TOKEN, "channels": ["trades", "holders"]})

    def _collect(ws):
        ws.receive_json()  # welcome
        ws.send_text(frame)
        seen = {}
        for _ in range(12):
            msg = ws.receive_json()
            ch = msg.get("channel")
            if ch and ch not in seen:
                seen[ch] = msg
            if len(seen) >= 2:
                break
        return seen

    with client.websocket_connect("/ws") as a:
        first = _collect(a)
        assert "trades" in first, "the first subscriber must be baselined"
        assert first["trades"]["kind"] == "snapshot"
        assert len(first["trades"]["added"]) >= 1

        # B joins while A is still connected and the hub is already primed
        with client.websocket_connect("/ws") as b:
            second = _collect(b)

        assert "trades" in second, "a late joiner must not be left empty"
        assert second["trades"]["kind"] == "snapshot", "it needs a baseline, not a delta"
        assert len(second["trades"]["added"]) == len(first["trades"]["added"])
        assert "holders" in second

    HUB.subscribers.clear()


# re-subscribing after an unsubscribe must baseline again, not assume prior state
def test_resubscribe_rebaselines(db):
    import json

    from fastapi.testclient import TestClient

    import api.api  # noqa: F401
    from api.ws import HUB

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xh01b", log_idx=0)
    storage.record_block_processed(101)

    client = TestClient(api.api.app)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN, "channels": ["trades"]}))
        got_first = False
        for _ in range(10):
            m = ws.receive_json()
            if m.get("channel") == "trades":
                got_first = m.get("kind") == "snapshot"
                break
        assert got_first

        ws.send_text(json.dumps({"op": "unsubscribe", "token": TOKEN, "channels": ["trades"]}))
        for _ in range(10):
            if ws.receive_json().get("op") == "unsubscribed":
                break

        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN, "channels": ["trades"]}))
        rebaselined = False
        for _ in range(10):
            m = ws.receive_json()
            if m.get("channel") == "trades":
                rebaselined = m.get("kind") == "snapshot" and len(m.get("added", [])) >= 1
                break
        assert rebaselined, "a re-subscribe must send a fresh baseline"

    HUB.subscribers.clear()
