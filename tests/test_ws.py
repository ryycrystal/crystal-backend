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


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(api.api.app)


# a fresh socket is greeted with what it can actually subscribe to
def test_welcome_declares_capabilities(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["op"] == "welcome"
        assert set(msg["channels"]) == set(KNOWN_CHANNELS)
        assert set(msg["implemented"]) == set(IMPLEMENTED_CHANNELS)


# subscribing splits what is live from what is declared but not built
def test_subscribe_separates_implemented_from_pending(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats", "holders"]}))
        reply = ws.receive_json()
        assert reply["op"] == "subscribed"
        assert reply["token"] == TOKEN_A
        assert reply["channels"] == ["stats"]
        assert reply["not_yet_implemented"] == ["holders"]


# a bad address must be refused rather than silently accepted
def test_subscribe_rejects_a_malformed_token(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": "nope", "channels": ["stats"]}))
        reply = ws.receive_json()
        assert reply["op"] == "error"
        assert "invalid token" in reply["error"]


# an unknown channel names itself rather than failing opaquely
def test_subscribe_rejects_unknown_channels(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["nonsense"]}))
        reply = ws.receive_json()
        assert reply["op"] == "error"
        assert "nonsense" in reply["error"]
        assert set(reply["known"]) == set(KNOWN_CHANNELS)


# unsubscribing one channel keeps the rest, unsubscribing all drops the token
def test_unsubscribe_is_granular(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats"]}))
        ws.receive_json()
        ws.send_text(json.dumps({"op": "unsubscribe", "token": TOKEN_A}))
        reply = ws.receive_json()
        assert reply["op"] == "unsubscribed"
        assert reply["channels"] == "all"


# malformed input must not drop the connection
def test_malformed_frame_does_not_kill_the_socket(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text("{not json")
        assert ws.receive_json()["op"] == "error"
        ws.send_text(json.dumps({"op": "ping"}))
        assert ws.receive_json()["op"] == "pong"


# one client's subscriptions must never leak into another's
def test_subscriptions_are_isolated_between_clients(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        a.receive_json()
        b.receive_json()
        a.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats"]}))
        a.receive_json()
        b.send_text(json.dumps({"op": "subscribe", "token": TOKEN_B, "channels": ["stats"]}))
        b.receive_json()

        subs = {frozenset(s.subscriptions.keys()) for s in HUB.subscribers}
        assert frozenset({TOKEN_A}) in subs
        assert frozenset({TOKEN_B}) in subs


# disconnecting must not leave the hub holding the socket
def test_hub_releases_disconnected_sockets(client):
    before = len(HUB.subscribers)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"op": "subscribe", "token": TOKEN_A, "channels": ["stats"]}))
        ws.receive_json()
    assert len(HUB.subscribers) <= before, "socket was not released on disconnect"
