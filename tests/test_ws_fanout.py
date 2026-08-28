import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.ws as ws
from api.ws import Hub, Subscriber

TOKEN = "0x" + "a" * 40


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class _CountingJson:
    def __init__(self, real) -> None:
        self.real = real
        self.dumps_calls = 0

    def dumps(self, obj, *args, **kwargs):
        self.dumps_calls += 1
        return self.real.dumps(obj, *args, **kwargs)

    def loads(self, *args, **kwargs):
        return self.real.loads(*args, **kwargs)


def _hub_with(n: int, channel: str = "trades") -> tuple[Hub, list[Subscriber]]:
    hub = Hub()
    subs = []
    for _ in range(n):
        sub = Subscriber(_FakeSocket())
        sub.subscriptions[TOKEN] = {channel}
        sub.primed.add((TOKEN, channel))
        hub.subscribers.add(sub)
        subs.append(sub)
    return hub, subs


def test_broadcast_gives_every_socket_its_own_seq_and_the_same_body():
    hub, subs = _hub_with(3)

    async def run():
        await hub.broadcast(TOKEN, "trades", {"op": "update", "added": [{"id": "1"}]})
        await hub.broadcast(TOKEN, "trades", {"op": "update", "added": [{"id": "2"}]})

    asyncio.run(run())

    for sub in subs:
        frames = [json.loads(t) for t in sub.socket.sent]
        assert [f["seq"] for f in frames] == [1, 2]
        assert frames[0]["op"] == "update"
        assert frames[0]["added"] == [{"id": "1"}]
        assert frames[1]["added"] == [{"id": "2"}]


def test_broadcast_serializes_the_body_once_no_matter_how_many_sockets(monkeypatch):
    hub, subs = _hub_with(5)
    shim = _CountingJson(json)
    monkeypatch.setattr(ws, "json", shim)

    asyncio.run(hub.broadcast(TOKEN, "trades", {"op": "update", "added": [{"id": "1"}]}))

    assert shim.dumps_calls == 1
    assert all(len(s.socket.sent) == 1 for s in subs)


def test_broadcast_of_an_empty_payload_is_still_valid_json():
    hub, subs = _hub_with(1)

    asyncio.run(hub.broadcast(TOKEN, "trades", {}))

    assert json.loads(subs[0].socket.sent[0]) == {"seq": 1}


def test_broadcast_preserves_nested_and_unicode_values():
    hub, subs = _hub_with(1)
    payload = {"op": "update", "rows": [{"name": "café ☕", "n": 1.5, "deep": {"a": [1, 2, None]}}]}

    asyncio.run(hub.broadcast(TOKEN, "trades", payload))

    frame = json.loads(subs[0].socket.sent[0])
    frame.pop("seq")
    assert frame == payload


def test_tick_call_runs_one_query_for_concurrent_callers():
    hub = Hub()
    calls = {"n": 0}

    def query():
        calls["n"] += 1
        return "value"

    async def run():
        return await asyncio.gather(*(hub._tick_call(("k",), query) for _ in range(5)))

    results = asyncio.run(run())

    assert calls["n"] == 1
    assert results == ["value"] * 5


def test_tick_call_does_not_serve_a_stale_result_to_a_later_tick():
    hub = Hub()
    calls = {"n": 0}

    def query():
        calls["n"] += 1
        return calls["n"]

    first = asyncio.run(hub._tick_call(("k",), query))
    second = asyncio.run(hub._tick_call(("k",), query))

    assert (first, second) == (1, 2)


def test_push_token_uses_the_watermark_from_the_tick(monkeypatch):
    hub, _ = _hub_with(1, channel="stats")
    seen: list[tuple[str, int]] = []

    async def fake_push(token: str, watermark: int) -> None:
        seen.append((token, watermark))

    def boom():
        raise AssertionError("_push_token must not query the watermark itself")

    monkeypatch.setattr(hub, "_push_stats", fake_push)
    monkeypatch.setattr("api.ws_data.indexer_watermark", boom)

    asyncio.run(hub._push_token(TOKEN, 4242))

    assert seen == [(TOKEN, 4242)]
