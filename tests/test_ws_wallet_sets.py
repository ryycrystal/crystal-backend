"""What a wallet-scoped websocket subscriber receives after its snapshot, and when its wallet set changes.

Every channel scoped to a wallet set (positions, user_positions, balances, the order book channels) must
send the snapshot once and then only what changed, must deliver a newly added wallet's rows as a delta
without re-sending the wallets already held, and must keep each socket's own baseline so two sockets
watching one wallet both see every change. No database: the row builders are stubbed.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.api  # noqa: F401,E402
import api.ws as ws  # noqa: E402
import api.ws_data as ws_data  # noqa: E402
from api.ws import Hub, Subscriber, _apply_subscribe  # noqa: E402

TOKEN = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
A = "0x000000000000000000000000000000000000aaaa"
B = "0x000000000000000000000000000000000000bbbb"


class Socket:
    def __init__(self):
        self.frames = []

    async def send_text(self, text):
        self.frames.append(json.loads(text))


class Rows:
    """Row builders whose output the tests can move between pushes."""

    def __init__(self):
        self.price = {A: "1.0", B: "2.0"}
        self.mon = {A: "10", B: "20"}

    def positions_for_wallets(self, addrs):
        return [{"address": a, "token": TOKEN, "balance_token": "5", "last_price_native": self.price[a]} for a in addrs]

    def positions_for(self, token, addrs):
        return [{"address": a, "token": token, "balance_token": "5", "last_price_native": self.price[a]} for a in addrs]

    def spot_body(self, wallet, include_zero=False):
        return {"wallet": wallet, "rows": [{"symbol": "MON", "balance": self.mon[wallet]}], "balance_block": 7}


def _hub(monkeypatch):
    rows = Rows()
    monkeypatch.setattr(ws_data, "positions_for_wallets", rows.positions_for_wallets)
    monkeypatch.setattr(ws_data, "positions_for", rows.positions_for)
    monkeypatch.setattr("api.spot_data.spot_body", rows.spot_body)
    monkeypatch.setattr(ws, "_watermark", lambda: 100)
    hub = Hub()
    hub._balances_checked.clear()
    return hub, rows


def _subscriber(hub, token, channel, addresses):
    sub = Subscriber(Socket())
    sub.subscriptions[token] = {channel}
    sub.addresses = set(addresses)
    hub.subscribers.add(sub)
    return sub


def frames(sub, channel):
    return [f for f in sub.socket.frames if f.get("channel") == channel]


def test_a_user_positions_snapshot_is_not_sent_again_on_the_first_push(monkeypatch):
    hub, _ = _hub(monkeypatch)
    sub = _subscriber(hub, "portfolio", "user_positions", [A])

    async def drive():
        await hub.send_snapshot(sub, "portfolio", "user_positions")
        await hub._push_user_positions("portfolio", 101)
        await hub._push_user_positions("portfolio", 102)

    asyncio.run(drive())
    sent = frames(sub, "user_positions")
    assert [f["kind"] for f in sent] == ["snapshot"], f"the snapshot was re-sent: {[f['kind'] for f in sent]}"
    assert [r["address"] for r in sent[0]["upserts"]] == [A]


def test_a_positions_snapshot_is_not_sent_again_on_the_first_push(monkeypatch):
    hub, _ = _hub(monkeypatch)
    sub = _subscriber(hub, TOKEN, "positions", [A])

    async def drive():
        await hub.send_snapshot(sub, TOKEN, "positions")
        await hub._push_positions(TOKEN, 101)

    asyncio.run(drive())
    assert [f["kind"] for f in frames(sub, "positions")] == ["snapshot"]


def test_a_balances_snapshot_is_not_sent_again_on_the_first_push(monkeypatch):
    hub, _ = _hub(monkeypatch)
    sub = _subscriber(hub, "portfolio", "balances", [A])

    async def drive():
        await hub.send_snapshot(sub, "portfolio", "balances")
        hub._balances_checked.clear()
        await hub._push_balances("portfolio", 101)

    asyncio.run(drive())
    sent = frames(sub, "balances")
    assert [f["kind"] for f in sent] == ["snapshot"], (
        f"an identical balance was pushed again: {[f['kind'] for f in sent]}"
    )
    assert sent[0]["wallets"][A]["rows"][0]["balance"] == "10"


def test_a_changed_balance_still_reaches_the_subscriber(monkeypatch):
    hub, rows = _hub(monkeypatch)
    sub = _subscriber(hub, "portfolio", "balances", [A])

    async def drive():
        await hub.send_snapshot(sub, "portfolio", "balances")
        rows.mon[A] = "11"
        hub._balances_checked.clear()
        await hub._push_balances("portfolio", 101)

    asyncio.run(drive())
    sent = frames(sub, "balances")
    assert [f["kind"] for f in sent] == ["snapshot", "delta"]
    assert sent[1]["wallets"][A]["rows"][0]["balance"] == "11"


def test_adding_a_wallet_delivers_only_that_wallets_rows_as_a_delta(monkeypatch):
    """The trader popup adds the trader's wallet to a socket that already streams the viewer's own
    portfolio. That must not re-send the viewer's positions and must not need a new snapshot."""
    hub, _ = _hub(monkeypatch)
    sub = _subscriber(hub, "portfolio", "user_positions", [A])

    async def drive():
        await hub.send_snapshot(sub, "portfolio", "user_positions")
        reply = await _apply_subscribe(
            sub, {"op": "subscribe", "token": "portfolio", "channels": ["user_positions"], "addresses": [A, B]}
        )
        assert reply["addresses"] == [A, B]
        assert ("portfolio", "user_positions") in sub.primed, "adding a wallet must not throw the baseline away"
        await hub._push_user_positions("portfolio", 101)

    asyncio.run(drive())
    sent = frames(sub, "user_positions")
    assert [f["kind"] for f in sent] == ["snapshot", "delta"], [f["kind"] for f in sent]
    assert [r["address"] for r in sent[1]["upserts"]] == [B], "only the added wallet's rows travel"
    assert not sent[1].get("removed")


def test_dropping_a_wallet_removes_its_rows_as_a_delta(monkeypatch):
    hub, _ = _hub(monkeypatch)
    sub = _subscriber(hub, "portfolio", "user_positions", [A, B])

    async def drive():
        await hub.send_snapshot(sub, "portfolio", "user_positions")
        await _apply_subscribe(
            sub, {"op": "subscribe", "token": "portfolio", "channels": ["user_positions"], "addresses": [A]}
        )
        await hub._push_user_positions("portfolio", 101)

    asyncio.run(drive())
    sent = frames(sub, "user_positions")
    assert [f["kind"] for f in sent] == ["snapshot", "delta"]
    assert sent[1]["upserts"] == []
    assert sent[1]["removed"] == [f"{B}:{TOKEN}"]


def test_a_late_socket_is_not_sent_a_balance_it_already_holds(monkeypatch):
    """A baseline kept per wallet rather than per socket meant a socket whose snapshot already carried
    the newest balance was sent it again on the next push, because the older socket had not seen it."""
    hub, rows = _hub(monkeypatch)
    first = _subscriber(hub, "portfolio", "balances", [A])
    second = _subscriber(hub, "portfolio", "balances", [A])

    async def drive():
        await hub.send_snapshot(first, "portfolio", "balances")
        rows.mon[A] = "11"
        await hub.send_snapshot(second, "portfolio", "balances")
        hub._balances_checked.clear()
        await hub._push_balances("portfolio", 101)

    asyncio.run(drive())
    assert [f["wallets"][A]["rows"][0]["balance"] for f in frames(first, "balances")] == ["10", "11"]
    assert [f["wallets"][A]["rows"][0]["balance"] for f in frames(second, "balances")] == ["11"], "already held"


def test_a_removed_subscriber_leaves_no_baseline_behind(monkeypatch):
    hub, _ = _hub(monkeypatch)
    sub = _subscriber(hub, "portfolio", "user_positions", [A])

    async def drive():
        await hub.send_snapshot(sub, "portfolio", "user_positions")
        await hub._push_user_positions("portfolio", 101)
        await hub.remove(sub)

    asyncio.run(drive())
    assert not [k for k in hub._prev_rows if str(id(sub)) in k[1]], "per-socket baselines must go with the socket"
