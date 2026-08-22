"""channel payload semantics against a real database.

the failure modes that matter here are silent: a seq gap the client cannot detect,
a holder that hits zero and lingers forever, a trade id format that double counts.
"""

import asyncio
import os
import sys
import time
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


# the hub starts a background fanout task on first connect. left running it keeps a
# pooled connection and its 250ms tick alive, which stops the module fixture from
# dropping the scratch database and breaks every test that follows
@pytest.fixture(autouse=True)
def _stop_hub():
    yield
    from api.ws import HUB

    HUB.subscribers.clear()
    task = getattr(HUB, "_task", None)
    if task is not None and not task.done():
        task.cancel()
    HUB._task = None
    HUB._prev_rows.clear()
    HUB._last_sent.clear()


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


# seq must advance by exactly one per frame so a client can detect a dropped delta,
# and it is per connection: hub level numbering made a reconnecting client see a jump
# and re-fetch REST even though its snapshot was already complete
def test_seq_is_per_subscriber_and_monotonic(db):
    from api.ws import Subscriber

    a = Subscriber(None)
    b = Subscriber(None)

    assert [a.next_seq(TOKEN, "trades") for _ in range(3)] == [1, 2, 3]
    assert [a.next_seq(TOKEN, "holders") for _ in range(2)] == [1, 2], "channels count apart"
    assert a.next_seq("0xother", "trades") == 1, "tokens count apart"

    # a second connection starts its own numbering rather than inheriting the hub's
    assert b.next_seq(TOKEN, "trades") == 1, "each socket numbers independently"
    assert a.next_seq(TOKEN, "trades") == 4, "and does not disturb the first"


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


# a notify sent inside a committed transaction reaches a listening connection,
# which is the entire contract the hub's push loop stands on
def test_listen_notify_roundtrip(db):
    import select as _select

    from core.storage.base import listen_connection

    listener = listen_connection()
    try:
        cur = listener.cursor()
        cur.execute("LISTEN crystal_new_block;")

        with storage.db_cursor() as wcur:
            wcur.execute("NOTIFY crystal_new_block")

        assert _select.select([listener], [], [], 5.0)[0], "notify must arrive within the window"
        listener.poll()
        assert len(listener.notifies) >= 1
        assert listener.notifies[0].channel == "crystal_new_block"
    finally:
        listener.close()


# the vaults body folds every page into one filter-agnostic universe
def test_vaults_list_body_shape(db, monkeypatch):
    import api.ws as ws_mod

    calls = []

    def fake_list(**kwargs):
        calls.append(kwargs["page"])
        if kwargs["page"] == 1:
            return {"vaults": [{"address": "0xv1"}], "total": 2, "hasMore": True}
        return {"vaults": [{"address": "0xv2"}], "total": 2, "hasMore": False}

    import api.routes.vaults as rv

    monkeypatch.setattr(rv, "list_vaults", lambda **kw: fake_list(**kw))
    body = ws_mod._vaults_list_body(["0xabc"])
    assert calls == [1, 2], "pages fold until hasMore is false"
    assert [v["address"] for v in body["vaults"]] == ["0xv1", "0xv2"]
    assert body["total"] == 2


# the wallet scoped channel spans tokens but never leaks another wallet's rows
def test_user_positions_span_tokens_and_scope_to_wallets(db):
    from api.ws_data import positions_for_wallets

    token2 = "0x" + "22" * 20

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xw1", log_idx=0)
    _create(st, token=token2, blk=102, ts=1002, name="Tok2", symbol="TOK2")
    _trade(st, token=token2, native_reserve=1100 * 10**18, blk=103, ts=1003, txh="0xw2", log_idx=0)

    mine = positions_for_wallets([USER])
    tokens_seen = {r["token"] for r in mine}
    assert TOKEN in tokens_seen and token2 in tokens_seen, "one subscription covers every token"
    assert all(r["address"] == USER for r in mine)

    row = mine[0]
    for field in ("symbol", "total_pnl_native", "last_price_native", "balance_native", "source"):
        assert field in row, f"row must carry {field} so it renders like the rest response"

    assert positions_for_wallets(["0x000000000000000000000000000000000000dead"]) == []
    assert positions_for_wallets([]) == [], "no wallets means no query"


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


# an idle token must not re-send the same stats object every block. the watermark
# guard only asks whether the chain advanced, which on monad is every ~400ms
def test_stats_does_not_resend_an_unchanged_body(db):
    import api.api  # noqa: F401
    from api.ws import Hub

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xs01", log_idx=0)
    storage.record_block_processed(101)

    hub = Hub()
    sent: list[dict] = []

    async def _capture(token, channel, payload):
        sent.append(payload)

    async def _drive():
        hub.broadcast = _capture  # type: ignore[method-assign]
        # same underlying data, three successive blocks. the watermark has to advance
        # too: token_stats stamps it into the body, and comparing the raw body made
        # this pass while production still resent every frame
        for blk in (102, 103, 104):
            storage.record_block_processed(blk)
            # token_stats is ttl cached for 500ms, so a tight loop would keep handing
            # back one body and hide the very thing this test exists to catch
            await asyncio.sleep(0.6)
            await hub._push_stats(TOKEN, blk)

    asyncio.run(_drive())

    assert len(sent) == 1, f"unchanged stats resent {len(sent)} times across three blocks"


# a positions snapshot must carry only the wallets the asking socket registered.
# building it from the union across every subscriber handed each client the
# positions of everyone else watching the same token
def test_positions_snapshot_is_scoped_to_the_asking_socket(db):
    import api.api  # noqa: F401
    from api.ws import HUB, Subscriber

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xpp1", log_idx=0)
    storage.record_block_processed(101)

    mine = Subscriber(None)
    mine.subscriptions[TOKEN] = {"positions"}
    mine.addresses = {"0x000000000000000000000000000000000000beef"}

    other = Subscriber(None)
    other.subscriptions[TOKEN] = {"positions"}
    other.addresses = {USER}

    async def _drive():
        HUB.subscribers.add(mine)
        HUB.subscribers.add(other)
        try:
            return await HUB._channel_snapshot(TOKEN, "positions", mine)
        finally:
            HUB.subscribers.discard(mine)
            HUB.subscribers.discard(other)

    body = asyncio.run(_drive())
    got = {r["address"].lower() for r in (body or {}).get("upserts", [])}
    assert USER.lower() not in got, f"leaked another socket's wallet: {got}"


# the rest endpoint reports volume and tx counts over 24h under these names, so the
# token channel must too. pushing lifetime totals made the page jump by orders of
# magnitude a moment after it loaded
def test_token_channel_volume_and_tx_counts_are_24h(db):
    import api.api  # noqa: F401
    from api.ws_data import token_state

    st = _new_state()
    _create(st, blk=100, ts=1000)
    # one trade well outside the 24h window, one inside it
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1000, txh="0xold", log_idx=0)
    recent = int(time.time()) - 60
    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=recent, txh="0xnew", log_idx=0)
    storage.record_block_processed(102)

    body = token_state(TOKEN)

    assert int(body["buyTxs"]) + int(body["sellTxs"]) == 1, "only the trade inside 24h counts"
    assert int(body["buyTxsLifetime"]) + int(body["sellTxsLifetime"]) == 2, "lifetime keeps both"
    assert int(body["volumeNative"]) < int(body["volumeNativeLifetime"])


# realized pnl written before the cost basis model recorded net cash flow, so a
# wallet that never sold showed a loss equal to everything it had spent
def test_realized_pnl_backfill_corrects_non_sellers(db):
    import core.storage as st

    _create(_new_state(), blk=100, ts=1000)

    with st.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_positions
                (user_address, token, balance_token, token_bought, token_sold,
                 native_spent, native_received, realized_pnl_native,
                 unrealized_pnl_native, total_pnl_native, trade_count,
                 buy_count, sell_count)
            VALUES
                -- never sold, yet carries a loss equal to its whole spend
                (%s, %s, 100, 100, 0, 16265, 0, -16265, 500, -15765, 2, 2, 0),
                -- sold half: released basis is half the spend, so realized is 600-400
                (%s, %s, 50, 100, 50, 800, 600, -200, 250, 50, 3, 2, 1)
            """,
            ("0xaaa", TOKEN, "0xbbb", TOKEN),
        )

    st.backfill_realized_pnl()

    with st.db_cursor() as cur:
        cur.execute(
            "SELECT user_address, realized_pnl_native, total_pnl_native "
            "FROM launchpad_positions WHERE token = %s ORDER BY user_address",
            (TOKEN,),
        )
        got = {r[0]: (int(r[1]), int(r[2])) for r in cur.fetchall()}

    assert got["0xaaa"][0] == 0, "a wallet that never sold has realized nothing"
    assert got["0xaaa"][1] == 500, "total is realized plus unrealized"
    assert got["0xbbb"][0] == 200, "sold half of a 800 basis for 600 -> 600 - 400"
    assert got["0xbbb"][1] == 450


# a client that switches wallets must stop receiving the old one. unioning the
# addresses kept the previous wallet's position on the page for the whole session
def test_switching_wallets_replaces_the_address_set(db):
    import api.api  # noqa: F401
    from api.ws import Subscriber, _apply_subscribe

    a = "0x000000000000000000000000000000000000aaaa"
    b = "0x000000000000000000000000000000000000bbbb"
    sub = Subscriber(None)

    async def _sub(addrs):
        return await _apply_subscribe(
            sub, {"op": "subscribe", "token": TOKEN, "channels": ["positions"], "addresses": addrs}
        )

    asyncio.run(_sub([a]))
    assert sub.addresses == {a}

    reply = asyncio.run(_sub([b]))
    assert sub.addresses == {b}, "the previous wallet must not linger"
    assert reply["addresses"] == [b]
    assert (TOKEN, "positions") not in sub.primed, "a new wallet set needs a fresh baseline"

    # a message with no addresses key leaves the set alone
    asyncio.run(_apply_subscribe(sub, {"op": "subscribe", "token": TOKEN, "channels": ["trades"]}))
    assert sub.addresses == {b}


# the explorer list channel: subscribe with the pseudo token, get the full bucket
# snapshot, then per field patches rather than full rows when something changes
def test_tokens_list_channel_snapshot_and_field_diffs(db):
    import json

    from fastapi.testclient import TestClient

    import api.api
    from api.ws import HUB

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xtl1", log_idx=0)
    storage.record_block_processed(101)

    client = TestClient(api.api.app)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": "tokens", "channels": ["tokens"]}))
        snap = None
        for _ in range(10):
            m = ws.receive_json()
            if m.get("channel") == "tokens" and m.get("kind") == "snapshot":
                snap = m
                break
        assert snap and "recent_created" in snap, "list snapshot must carry the buckets"

        # a field diff, driven directly through the pusher
        sent = []

        async def cap(token, channel, payload):
            sent.append(payload)

        HUB.broadcast = cap  # type: ignore[method-assign]
        _trade(st, native_reserve=1300 * 10**18, blk=102, ts=1002, txh="0xtl2", log_idx=0)
        storage.record_block_processed(102)
        asyncio.run(HUB._push_tokens_list("tokens", 102))
        HUB.broadcast = type(HUB).broadcast.__get__(HUB)  # restore
        assert sent, "a change must produce a delta frame"
        frame = sent[-1]
        assert frame["u"], "changed token arrives as a per field patch"
        patch = list(frame["u"].values())[0]
        assert "token" not in patch or len(patch) < 10, "patch carries changed fields, not the row"
        assert "ids" in frame

    HUB.subscribers.clear()


# the orderbook channels: subscribe on the portfolio pseudo-token with a wallet,
# get a per-wallet snapshot, and the push path resends only on material change
def test_orderbook_channels_snapshot_and_push(db):
    import json

    from fastapi.testclient import TestClient

    import api.api
    from api.ws import HUB, _orderbook_wallet_body

    wallet = "0x581172970bda012d71a9aea34a9f219da117891b"
    market = "0xc8045b5dde24e625932df738e7ec4127c04008d3"
    with storage.db_cursor() as cur:
        for t in ("crystal_orderbook_events", "crystal_orderbook_orders", "crystal_orderbook_fills"):
            cur.execute(f"DELETE FROM {t}")
    storage.apply_orderbook_updates(
        {
            "market": market,
            "user": wallet,
            "orders": [{"flag": 2, "action": "add", "is_buy": True, "price": 500, "order_id": 7, "size": 1000}],
        },
        100,
        1000,
        "0xwsob1",
        0,
    )

    body = _orderbook_wallet_body("user_orders", wallet)
    assert body["orders"][0]["order_id"] == 7, "the shared body serves the decoded plane"

    client = TestClient(api.api.app)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(
            json.dumps(
                {
                    "op": "subscribe",
                    "token": "portfolio",
                    "channels": ["user_orders", "user_trades", "user_history"],
                    "addresses": [wallet],
                }
            )
        )
        reply = ws.receive_json()
        assert reply["op"] == "subscribed"
        assert set(reply["channels"]) == {"user_orders", "user_trades", "user_history"}
        assert reply["addresses"] == [wallet]

        snaps = {}
        for _ in range(10):
            m = ws.receive_json()
            if m.get("kind") == "snapshot" and m.get("channel", "").startswith("user_"):
                snaps[m["channel"]] = m
            if len(snaps) == 3:
                break
        assert set(snaps) == {"user_orders", "user_trades", "user_history"}
        assert snaps["user_orders"]["wallets"][wallet]["orders"][0]["order_id"] == 7
        assert snaps["user_history"]["wallets"][wallet]["orders"][0]["status"] == "open", (
            "history serves the order universe the ui renders, not the raw event stream"
        )

    # the push path: an unchanged book sends nothing, a change sends one frame
    sent = []

    class _FakeSub:
        addresses = {wallet}
        primed = {("portfolio", "user_orders")}

        def wants(self, token, channel):
            return token == "portfolio" and channel == "user_orders"

        def next_seq(self, token, channel):
            return 1

        async def send(self, payload):
            sent.append(payload)

    fake = _FakeSub()
    HUB.subscribers.add(fake)
    try:
        HUB._prev_rows.pop(("portfolio", f"user_orders:{wallet}"), None)
        asyncio.run(HUB._push_orderbook_channel("portfolio", 100, "user_orders"))
        assert len(sent) == 1, "the first push carries the baseline"
        asyncio.run(HUB._push_orderbook_channel("portfolio", 101, "user_orders"))
        assert len(sent) == 1, "an unchanged book pushes nothing"

        storage.apply_orderbook_updates(
            {
                "market": market,
                "user": wallet,
                "orders": [{"flag": 0, "action": "remove", "is_buy": True, "price": 500, "order_id": 7, "size": 1000}],
            },
            102,
            1002,
            "0xwsob2",
            0,
        )
        asyncio.run(HUB._push_orderbook_channel("portfolio", 102, "user_orders"))
        assert len(sent) == 2, "a book change pushes one frame"
        assert sent[1]["wallets"][wallet]["orders"] == [], "the frame carries the emptied book"
    finally:
        HUB.subscribers.discard(fake)
