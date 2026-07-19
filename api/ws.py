# websocket transport for live token data
#
# this module owns the connection layer only: accepting sockets, tracking who is
# subscribed to what, and fanning out payloads. it deliberately does not define
# channel contents beyond the first proof channel, because the channel design is
# still being specified by the frontend
#
# how updates reach here: a single background task polls the indexer watermark and
# recomputes a channel only when the watermark advances, so cost is a function of
# chain activity rather than of how many clients are connected. postgres
# LISTEN/NOTIFY from the indexer is the natural upgrade and would drop the last
# ~250ms of latency, but it needs a change on the indexer write path, which is
# deliberately left alone here

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# channels a client may subscribe to. stats is implemented; the rest are declared
# so a client subscribing early gets an explicit "not yet" rather than silence
KNOWN_CHANNELS = ("stats", "trades", "holders", "positions", "top_traders", "dev_tokens")
IMPLEMENTED_CHANNELS = ("stats",)

# how often the fanout task checks whether the indexer has moved
POLL_INTERVAL_SECONDS = 0.25

# a socket that has not pinged or subscribed in this long is dropped
IDLE_TIMEOUT_SECONDS = 300


# one connected client and everything it asked for
class Subscriber:
    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket
        # token -> set of channel names
        self.subscriptions: dict[str, set[str]] = {}
        self.last_seen = time.time()
        self.send_lock = asyncio.Lock()

    # tokens this client cares about
    def tokens(self) -> set[str]:
        return set(self.subscriptions.keys())

    # true when the client wants this channel for this token
    def wants(self, token: str, channel: str) -> bool:
        return channel in self.subscriptions.get(token, ())

    # serialise sends so two fanouts cannot interleave frames on one socket
    async def send(self, payload: dict[str, Any]) -> bool:
        async with self.send_lock:
            try:
                await self.socket.send_text(json.dumps(payload))
                return True
            except Exception:
                return False


# tracks every live socket and which tokens are worth computing
class Hub:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()
        self.lock = asyncio.Lock()
        # token -> last watermark we broadcast for, so we only push on change
        self._last_sent: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    # register a new socket
    async def add(self, sub: Subscriber) -> None:
        async with self.lock:
            self.subscribers.add(sub)
            self._ensure_task()

    # drop a socket and forget any token nobody is watching any more
    async def remove(self, sub: Subscriber) -> None:
        async with self.lock:
            self.subscribers.discard(sub)
            live = set()
            for s in self.subscribers:
                live |= s.tokens()
            for token in list(self._last_sent):
                if token not in live:
                    self._last_sent.pop(token, None)

    # every token at least one client is subscribed to
    async def active_tokens(self) -> set[str]:
        async with self.lock:
            out: set[str] = set()
            for s in self.subscribers:
                out |= s.tokens()
            return out

    # start the fanout loop the first time anyone connects
    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    # push a payload to every client subscribed to this token and channel
    async def broadcast(self, token: str, channel: str, payload: dict[str, Any]) -> None:
        async with self.lock:
            targets = [s for s in self.subscribers if s.wants(token, channel)]
        dead = [s for s in targets if not await s.send(payload)]
        for s in dead:
            await self.remove(s)

    # recompute subscribed tokens whenever the indexer watermark advances
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                tokens = await self.active_tokens()
                if not tokens:
                    continue
                for token in tokens:
                    await self._maybe_push_stats(token)
            except asyncio.CancelledError:
                raise
            except Exception:
                # a failure here must never kill the fanout loop
                continue

    # send stats for one token, but only if it actually changed
    async def _maybe_push_stats(self, token: str) -> None:
        from api.routes.launchpad import token_stats

        body = await asyncio.to_thread(token_stats, token)
        watermark = int(body.get("as_of_block") or 0)
        if self._last_sent.get(token) == watermark:
            return
        self._last_sent[token] = watermark
        await self.broadcast(token, "stats", {"channel": "stats", **body})


HUB = Hub()


# validate and apply one subscribe frame, returning the reply to send back
async def _apply_subscribe(sub: Subscriber, msg: dict[str, Any]) -> dict[str, Any]:
    token = str(msg.get("token") or "").lower().strip()
    channels = msg.get("channels") or []
    if not token.startswith("0x") or len(token) != 42:
        return {"op": "error", "error": "invalid token address"}
    if not isinstance(channels, list) or not channels:
        return {"op": "error", "error": "channels must be a non-empty list"}

    unknown = [c for c in channels if c not in KNOWN_CHANNELS]
    if unknown:
        return {"op": "error", "error": f"unknown channels: {unknown}", "known": list(KNOWN_CHANNELS)}

    accepted = [c for c in channels if c in IMPLEMENTED_CHANNELS]
    pending = [c for c in channels if c not in IMPLEMENTED_CHANNELS]
    sub.subscriptions.setdefault(token, set()).update(accepted)

    return {
        "op": "subscribed",
        "token": token,
        "channels": accepted,
        "not_yet_implemented": pending,
    }


# remove channels, and the token entirely once nothing is left on it
async def _apply_unsubscribe(sub: Subscriber, msg: dict[str, Any]) -> dict[str, Any]:
    token = str(msg.get("token") or "").lower().strip()
    channels = msg.get("channels")
    if token not in sub.subscriptions:
        return {"op": "unsubscribed", "token": token, "channels": []}
    if not channels:
        sub.subscriptions.pop(token, None)
        return {"op": "unsubscribed", "token": token, "channels": "all"}
    sub.subscriptions[token] -= set(channels)
    if not sub.subscriptions[token]:
        sub.subscriptions.pop(token, None)
    return {"op": "unsubscribed", "token": token, "channels": channels}


# live token data socket, one connection carries any number of token subscriptions
@router.websocket("/ws")
async def websocket_endpoint(socket: WebSocket) -> None:
    await socket.accept()
    sub = Subscriber(socket)
    await HUB.add(sub)

    await sub.send(
        {
            "op": "welcome",
            "channels": list(KNOWN_CHANNELS),
            "implemented": list(IMPLEMENTED_CHANNELS),
            "poll_interval_ms": int(POLL_INTERVAL_SECONDS * 1000),
        }
    )

    try:
        while True:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=IDLE_TIMEOUT_SECONDS)
            sub.last_seen = time.time()
            try:
                msg = json.loads(raw)
            except Exception:
                await sub.send({"op": "error", "error": "malformed json"})
                continue

            op = str(msg.get("op") or "").lower()
            if op == "subscribe":
                await sub.send(await _apply_subscribe(sub, msg))
            elif op == "unsubscribe":
                await sub.send(await _apply_unsubscribe(sub, msg))
            elif op == "ping":
                await sub.send({"op": "pong", "ts": int(time.time())})
            else:
                await sub.send({"op": "error", "error": f"unknown op: {op}"})
    except (TimeoutError, WebSocketDisconnect):
        pass
    except Exception:
        pass
    finally:
        await HUB.remove(sub)
        with contextlib.suppress(Exception):
            await socket.close()
