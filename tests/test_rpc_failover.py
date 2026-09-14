"""A node that stops answering must not stop the indexer or the api: the next node takes the call, the
dead one is set aside for a cooldown and then tried first again, and a JSON-RPC error body, which is a
real answer, never causes a failover."""

import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import rpc  # noqa: E402

PRIMARY = "https://primary.test"
FALLBACK = "https://fallback.test"


class Reply:
    def __init__(self, body, status=200, url=""):
        self._body = body
        self.status_code = status
        self.url = url
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            message = f"Client error '{self.status_code}' for url '{self.url}'"
            raise httpx.HTTPStatusError(message, request=None, response=self)

    def json(self):
        return self._body


class Node:
    """A fake network: which urls are dead, which answer, and who was asked."""

    def __init__(self, dead=(), answers=None, status=None):
        self.dead = set(dead)
        self.answers = answers or {}
        self.status = status or {}
        self.asked: list[str] = []

    def post(self, url, json=None, timeout=None):
        self.asked.append(url)
        if url in self.dead:
            raise httpx.ConnectError("refused", request=None)
        return Reply(self.answers.get(url, {"result": url}), self.status.get(url, 200), url)

    async def apost(self, url, json=None):
        return self.post(url, json=json)


def test_a_dead_primary_fails_over_and_comes_back_after_the_cooldown(monkeypatch):
    node = Node(dead={PRIMARY})
    monkeypatch.setattr(rpc.httpx, "post", node.post)
    clock = {"now": 1000.0}
    monkeypatch.setattr(rpc.time, "monotonic", lambda: clock["now"])
    endpoints = rpc.Endpoints([PRIMARY, FALLBACK], cooldown=60)

    assert rpc.post({"method": "eth_blockNumber"}, endpoints=endpoints) == {"result": FALLBACK}
    assert node.asked == [PRIMARY, FALLBACK]
    assert endpoints.ordered() == [FALLBACK, PRIMARY], "the dead node moves last while it cools down"

    node.asked.clear()
    rpc.post({"method": "eth_blockNumber"}, endpoints=endpoints)
    assert node.asked == [FALLBACK], "while cooling down the primary is not even asked"

    clock["now"] += 61
    node.dead.clear()
    node.asked.clear()
    assert rpc.post({"method": "eth_blockNumber"}, endpoints=endpoints) == {"result": PRIMARY}
    assert node.asked == [PRIMARY], "after the cooldown the primary is tried first and wins"


def test_a_json_rpc_error_body_is_the_answer_not_a_dead_node(monkeypatch):
    node = Node(answers={PRIMARY: {"error": {"code": -32602, "message": "invalid params"}}})
    monkeypatch.setattr(rpc.httpx, "post", node.post)
    endpoints = rpc.Endpoints([PRIMARY, FALLBACK], cooldown=60)

    body = rpc.post({"method": "eth_call"}, endpoints=endpoints)
    assert body["error"]["message"] == "invalid params"
    assert node.asked == [PRIMARY], "the fallback is never asked to repeat a question the node answered"
    assert endpoints.ordered() == [PRIMARY, FALLBACK]
    with pytest.raises(RuntimeError, match="invalid params"):
        rpc.call("eth_call", [], endpoints=endpoints)


def test_a_non_2xx_status_counts_as_a_dead_node(monkeypatch):
    node = Node(status={PRIMARY: 503})
    monkeypatch.setattr(rpc.httpx, "post", node.post)
    endpoints = rpc.Endpoints([PRIMARY, FALLBACK], cooldown=60)
    assert rpc.post({"method": "eth_blockNumber"}, endpoints=endpoints) == {"result": FALLBACK}


def test_every_node_down_raises_one_clear_error(monkeypatch):
    node = Node(dead={PRIMARY, FALLBACK})
    monkeypatch.setattr(rpc.httpx, "post", node.post)
    endpoints = rpc.Endpoints([PRIMARY, FALLBACK], cooldown=60)
    with pytest.raises(rpc.RpcUnavailable):
        rpc.post({"method": "eth_blockNumber"}, endpoints=endpoints)
    assert node.asked == [PRIMARY, FALLBACK]


def test_a_node_is_named_by_host_only_in_the_log_and_the_error(monkeypatch, capsys):
    """A paid node's key rides in the path of its url, and both the log line and the error used to
    carry the whole url, so one failover put the key in the container logs."""
    keyed = "https://paid.test/v2/SECRET-KEY-123"
    node = Node(status={keyed: 403}, dead={FALLBACK}, answers={keyed: {"error": {"message": "origin not allowed"}}})
    monkeypatch.setattr(rpc.httpx, "post", node.post)
    endpoints = rpc.Endpoints([keyed, FALLBACK], cooldown=60)

    with pytest.raises(rpc.RpcUnavailable) as err:
        rpc.post({"method": "eth_blockNumber"}, endpoints=endpoints)

    out = capsys.readouterr().out
    assert "SECRET-KEY-123" not in out, out
    assert "SECRET-KEY-123" not in str(err.value), str(err.value)
    assert "paid.test failed (HTTP 403" in out and "origin not allowed" in out
    assert "fallback.test" in str(err.value)


def test_the_configured_list_is_primary_then_fallbacks_without_repeats(monkeypatch):
    monkeypatch.setenv("RPC_HTTP", "https://paid.test")
    monkeypatch.setenv("RPC_HTTP_FALLBACKS", "https://a.test, https://paid.test ,https://b.test")
    assert rpc.configured_urls() == ["https://paid.test", "https://a.test", "https://b.test"]
    monkeypatch.delenv("RPC_HTTP_FALLBACKS")
    assert rpc.configured_urls() == ["https://paid.test", *rpc.DEFAULT_FALLBACKS]


def test_the_indexers_rpc_helper_survives_a_dead_primary(monkeypatch):
    """backfill.http_jsonrpc is what every live block fetch goes through."""
    import backfill

    node = Node(dead={PRIMARY}, answers={FALLBACK: {"result": "0x10"}})
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda self, url, json=None: node.apost(url, json=json))
    monkeypatch.setattr(rpc, "ENDPOINTS", rpc.Endpoints([PRIMARY, FALLBACK], cooldown=60))
    monkeypatch.setattr(backfill, "RPC_HTTP", PRIMARY, raising=False)

    async def gate():
        return None

    monkeypatch.setattr(backfill.h, "rate_gate", gate)
    assert asyncio.run(backfill.get_head_http()) == 16
    assert node.asked == [PRIMARY, FALLBACK]
