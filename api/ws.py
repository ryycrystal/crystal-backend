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
IMPLEMENTED_CHANNELS = ("stats", "trades", "holders", "positions", "top_traders", "dev_tokens")

# channels that send the whole object every time rather than a diff
SNAPSHOT_CHANNELS = ("stats", "dev_tokens")

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
        # wallets this client wants positions for, positions are per wallet
        self.addresses: set[str] = set()
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
        # (token, channel) -> last watermark broadcast, so we only push on change
        self._last_sent: dict[tuple[str, str], int] = {}
        # (token, channel) -> monotonic frame counter. a client that sees a gap knows
        # it missed a delta and must re-baseline, which is the only thing that makes
        # delta channels safe across a reconnect
        self._seq: dict[tuple[str, str], int] = {}
        # (token, channel) -> last emitted rows, keyed, so deltas can be diffed
        self._prev_rows: dict[tuple[str, str], dict[str, dict]] = {}
        self._task: asyncio.Task | None = None

    # next frame number for a channel
    def _next_seq(self, token: str, channel: str) -> int:
        key = (token, channel)
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

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
            for key in list(self._last_sent):
                if key[0] not in live:
                    self._last_sent.pop(key, None)
                    self._prev_rows.pop(key, None)

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
                    await self._push_token(token)
            except asyncio.CancelledError:
                raise
            except Exception:
                # a failure here must never kill the fanout loop
                continue

    # push every channel that has a subscriber and whose data actually moved
    async def _push_token(self, token: str) -> None:
        from api.ws_data import indexer_watermark

        watermark = await asyncio.to_thread(indexer_watermark)
        async with self.lock:
            wanted: set[str] = set()
            for sub in self.subscribers:
                wanted |= sub.subscriptions.get(token, set())

        for channel in wanted:
            key = (token, channel)
            # positions depend on the wallet set, so they cannot be skipped on
            # watermark alone -- a client can add an address at any time
            if channel != "positions" and self._last_sent.get(key) == watermark:
                continue
            self._last_sent[key] = watermark
            fn = {
                "stats": self._push_stats,
                "trades": self._push_trades,
                "holders": self._push_holders,
                "top_traders": self._push_top_traders,
                "dev_tokens": self._push_dev_tokens,
                "positions": self._push_positions,
            }.get(channel)
            if fn is not None:
                await fn(token, watermark)

    # wrap a channel body in the envelope every frame carries
    def _envelope(self, token: str, channel: str, watermark: int, kind: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "token": token,
            "as_of_block": watermark,
            "seq": self._next_seq(token, channel),
            "kind": kind,
        }

    # diff keyed rows against what was last sent, returning upserts and removals
    def _diff_rows(self, token: str, channel: str, rows: dict[str, dict]) -> tuple[list[dict], list[str]]:
        key = (token, channel)
        prev = self._prev_rows.get(key)
        self._prev_rows[key] = rows
        if prev is None:
            # first frame for this channel is everything, sent as a snapshot
            return list(rows.values()), []
        upserts = [v for k, v in rows.items() if prev.get(k) != v]
        removed = [k for k in prev if k not in rows]
        return upserts, removed

    # stats: an aggregate, so the whole object every time
    async def _push_stats(self, token: str, watermark: int) -> None:
        from api.routes.launchpad import token_stats

        body = await asyncio.to_thread(token_stats, token)
        env = self._envelope(token, "stats", watermark, "snapshot")
        await self.broadcast(token, "stats", {**env, **body})

    # trades: append only, ids are stable, removals happen only on a reorg
    async def _push_trades(self, token: str, watermark: int) -> None:
        from api.ws_data import recent_trades

        rows = await asyncio.to_thread(recent_trades, token)
        keyed = {r["id"]: r for r in rows}
        added, removed = self._diff_rows(token, "trades", keyed)
        if not added and not removed:
            return
        first = len(self._prev_rows.get((token, "trades"), {})) == len(added) and not removed
        env = self._envelope(token, "trades", watermark, "snapshot" if first else "delta")
        payload = {**env, "added": added}
        if removed:
            payload["removed"] = removed
        await self.broadcast(token, "trades", payload)

    # holders: upsert by address, and a holder reaching zero must be removed
    # explicitly or it lingers in the client list forever
    async def _push_holders(self, token: str, watermark: int) -> None:
        from api.ws_data import top_holders

        rows = await asyncio.to_thread(top_holders, token)
        keyed = {r["address"]: r for r in rows}
        upserts, removed = self._diff_rows(token, "holders", keyed)
        if not upserts and not removed:
            return
        env = self._envelope(token, "holders", watermark, "delta")
        payload = {**env, "upserts": upserts}
        if removed:
            payload["removed"] = removed
        await self.broadcast(token, "holders", payload)

    # top traders: same rows as holders but ordered by the client, which is the only
    # side that knows the live price
    async def _push_top_traders(self, token: str, watermark: int) -> None:
        from api.ws_data import top_traders

        rows = await asyncio.to_thread(top_traders, token)
        keyed = {r["address"]: r for r in rows}
        upserts, removed = self._diff_rows(token, "top_traders", keyed)
        if not upserts and not removed:
            return
        env = self._envelope(token, "top_traders", watermark, "delta")
        payload = {**env, "upserts": upserts}
        if removed:
            payload["removed"] = removed
        await self.broadcast(token, "top_traders", payload)

    # dev tokens: small and slow moving, so the whole array on change
    async def _push_dev_tokens(self, token: str, watermark: int) -> None:
        from api.ws_data import dev_tokens

        rows = await asyncio.to_thread(dev_tokens, token)
        key = (token, "dev_tokens")
        if self._prev_rows.get(key) == {"all": {"rows": rows}}:
            return
        self._prev_rows[key] = {"all": {"rows": rows}}
        env = self._envelope(token, "dev_tokens", watermark, "snapshot")
        await self.broadcast(token, "dev_tokens", {**env, "devTokens": rows})

    # positions are per wallet, so they fan out per subscriber rather than per token
    async def _push_positions(self, token: str, watermark: int) -> None:
        from api.ws_data import positions_for

        async with self.lock:
            targets = [s for s in self.subscribers if s.wants(token, "positions") and s.addresses]
        for sub in targets:
            wanted = sorted(sub.addresses)
            rows = await asyncio.to_thread(positions_for, token, wanted)
            keyed = {r["address"]: r for r in rows}
            key = (token, f"positions:{id(sub)}")
            prev = self._prev_rows.get(key)
            self._prev_rows[key] = keyed
            if prev is None:
                upserts, removed = list(keyed.values()), []
            else:
                upserts = [v for k, v in keyed.items() if prev.get(k) != v]
                removed = [k for k in prev if k not in keyed]
            if not upserts and not removed:
                continue
            env = self._envelope(token, "positions", watermark, "delta")
            payload = {**env, "upserts": upserts}
            if removed:
                payload["removed"] = removed
            await sub.send(payload)


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

    addresses = msg.get("addresses") or []
    if isinstance(addresses, list):
        for a in addresses:
            a = str(a or "").lower().strip()
            if a.startswith("0x") and len(a) == 42:
                sub.addresses.add(a)

    reply = {
        "op": "subscribed",
        "token": token,
        "channels": accepted,
        "not_yet_implemented": pending,
    }
    if "positions" in accepted:
        reply["addresses"] = sorted(sub.addresses)
        if not sub.addresses:
            reply["warning"] = "positions needs an addresses array, none were accepted"
    return reply


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
