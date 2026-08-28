"""websocket connection layer.

covers the transport only: accept, subscribe semantics, isolation between
clients and push-on-change. channel contents are still being specified.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.api  # noqa: F401,E402
from api.ws import HUB, IMPLEMENTED_CHANNELS, KNOWN_CHANNELS  # noqa: E402

TOKEN_A = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
TOKEN_B = "0x2c6dd544e63cae0330b5edc5f0bcd108652c8888"


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


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(api.api.app)


def _next_op(ws):
    for _ in range(20):
        msg = ws.receive_json()
        if msg.get("op"):
            return msg
    raise AssertionError("no op frame received")


def test_welcome_declares_capabilities(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["op"] == "welcome"
        assert set(msg["channels"]) == set(KNOWN_CHANNELS)
        assert set(msg["implemented"]) == set(IMPLEMENTED_CHANNELS)


def test_subscribe_accepts_all_declared_channels(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": list(KNOWN_CHANNELS)}))
        reply = ws.receive_json()
        assert reply["op"] == "subscribed"
        assert reply["token"] == TOKEN_A
        assert set(reply["channels"]) == set(KNOWN_CHANNELS)
        assert reply["not_yet_implemented"] == []


def test_positions_subscription_carries_addresses(client):
    wallet = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(
            json.dumps(
                {
                    "op": "subscribe",
                    "token": TOKEN_A,
                    "channels": ["positions"],
                    "addresses": [wallet, "not-an-address"],
                }
            )
        )
        reply = ws.receive_json()
        assert reply["addresses"] == [wallet], "malformed addresses must be dropped"
        assert "warning" not in reply


def test_positions_without_addresses_warns(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["positions"]}))
        reply = ws.receive_json()
        assert reply["addresses"] == []
        assert "warning" in reply


def test_subscribe_rejects_a_malformed_token(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": "nope", "channels": ["stats"]}))
        reply = ws.receive_json()
        assert reply["op"] == "error"
        assert "invalid token" in reply["error"]


def test_subscribe_rejects_unknown_channels(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["nonsense"]}))
        reply = ws.receive_json()
        assert reply["op"] == "error"
        assert "nonsense" in reply["error"]
        assert set(reply["known"]) == set(KNOWN_CHANNELS)


def test_unsubscribe_is_granular(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats"]}))
        ws.receive_json()
        ws.send_text(json.dumps({"op": "unsubscribe", "token": TOKEN_A}))
        reply = _next_op(ws)
        assert reply["op"] == "unsubscribed"
        assert reply["channels"] == "all"


def test_malformed_frame_does_not_kill_the_socket(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text("{not json")
        assert ws.receive_json()["op"] == "error"
        ws.send_text(json.dumps({"op": "ping"}))
        assert ws.receive_json()["op"] == "pong"


def test_subscriptions_are_isolated_between_clients(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        a.receive_json()
        b.receive_json()
        a.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats"]}))
        _next_op(a)
        b.send_text(json.dumps({"op": "subscribe", "token": TOKEN_B, "channels": ["stats"]}))
        _next_op(b)

        subs = {frozenset(s.subscriptions.keys()) for s in HUB.subscribers}
        assert frozenset({TOKEN_A}) in subs
        assert frozenset({TOKEN_B}) in subs


def test_hub_releases_disconnected_sockets(client):
    before = len(HUB.subscribers)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats"]}))
        ws.receive_json()
    assert len(HUB.subscribers) <= before, "socket was not released on disconnect"
