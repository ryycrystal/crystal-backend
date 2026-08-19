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
KNOWN_CHANNELS = ("token", "stats", "trades", "holders", "positions", "top_traders", "dev_tokens", "tokens")
IMPLEMENTED_CHANNELS = ("token", "stats", "trades", "holders", "positions", "top_traders", "dev_tokens", "tokens")

# channels that send the whole object every time rather than a diff
SNAPSHOT_CHANNELS = ("token", "stats", "dev_tokens")

# how often the fanout task checks whether the indexer has moved
# one tick per monad block: pushing faster than blocks land is pure waste
POLL_INTERVAL_SECONDS = 0.4

# a socket that has not pinged or subscribed in this long is dropped
IDLE_TIMEOUT_SECONDS = 300

# tokens are refreshed concurrently, but bounded: the connection pool is 25 and one
# token sweep uses several, so unbounded fanout would starve the rest of the api
MAX_CONCURRENT_TOKEN_PUSHES = 6

# how many times a snapshot read is retried when a block lands mid-query. bounded so
# a busy token cannot spin here forever, the last attempt is served either way
SNAPSHOT_READ_ATTEMPTS = 3

# stamped into the stats body by the rest handler, so they move every block regardless
# of whether the token did. excluded when deciding if a stats frame is worth sending
_STATS_VOLATILE = frozenset({"as_of_block", "as_of_ts"})


# one connected client and everything it asked for
class Subscriber:
    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket
        # token -> set of channel names
        self.subscriptions: dict[str, set[str]] = {}
        # wallets this client wants positions for, positions are per wallet
        self.addresses: set[str] = set()
        # (token, channel) this socket has already received a baseline for. hub level
        # diff state cannot answer this: a client joining a token another client is
        # already watching has no state of its own and must be sent a full snapshot
        self.primed: set[tuple[str, str]] = set()
        # per connection frame counter. hub level numbering made a reconnecting
        # client see a jump and re-fetch REST even though its snapshot was complete
        self._seq: dict[tuple[str, str], int] = {}
        self.last_seen = time.time()
        self.send_lock = asyncio.Lock()

    # next frame number for this socket on this channel
    def next_seq(self, token: str, channel: str) -> int:
        key = (token, channel)
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

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
        # (token, channel) -> last emitted rows, keyed, so deltas can be diffed
        self._prev_rows: dict[tuple[str, str], dict[str, dict]] = {}
        self._task: asyncio.Task | None = None
        self._push_sem = asyncio.Semaphore(MAX_CONCURRENT_TOKEN_PUSHES)

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
            if not self.subscribers:
                self._stop_task()

    # stop the fanout loop once nobody is listening. left running it ticks every
    # 250ms and holds a pooled connection for no reader
    def _stop_task(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

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

    # push a delta to clients that already hold a baseline. a client without one is
    # skipped here and gets a snapshot from send_snapshot instead
    async def broadcast(self, token: str, channel: str, payload: dict[str, Any]) -> None:
        async with self.lock:
            targets = [s for s in self.subscribers if s.wants(token, channel) and (token, channel) in s.primed]
        dead = []
        for s in targets:
            framed = {**payload, "seq": s.next_seq(token, channel)}
            if not await s.send(framed):
                dead.append(s)
        for s in dead:
            await self.remove(s)

    # send one subscriber the current full state of a channel and mark it primed.
    # called on subscribe so a late joiner never waits for the next change
    async def send_snapshot(self, sub: Subscriber, token: str, channel: str) -> None:
        # a block landing while the snapshot query runs is broadcast only to sockets
        # already primed, and the hub's diff state has moved past it, so this socket
        # would never see that change again. retry until the watermark holds still
        # across the read, which means no delta could have been missed
        for _ in range(SNAPSHOT_READ_ATTEMPTS):
            before = await asyncio.to_thread(_watermark)
            body = await self._channel_snapshot(token, channel, sub)
            if body is None:
                return
            after = await asyncio.to_thread(_watermark)
            if after == before:
                break
        env = self._envelope(token, channel, before, "snapshot")
        frame = {**env, **body, "seq": sub.next_seq(token, channel)}
        if await sub.send(frame):
            sub.primed.add((token, channel))

    # the full current contents of one channel, shaped exactly like its push
    async def _channel_snapshot(self, token: str, channel: str, sub: Subscriber) -> dict[str, Any] | None:
        from api import ws_data as d

        if channel == "stats":
            from api.routes.launchpad import token_stats

            return await asyncio.to_thread(token_stats, token)
        if channel == "token":
            body = await asyncio.to_thread(d.token_state, token)
            return body or None
        if channel == "trades":
            return {"added": await asyncio.to_thread(d.recent_trades, token)}
        if channel == "holders":
            return {"upserts": await asyncio.to_thread(d.top_holders, token)}
        if channel == "top_traders":
            return {"upserts": await asyncio.to_thread(d.top_traders, token)}
        if channel == "dev_tokens":
            return {"devTokens": await asyncio.to_thread(d.dev_tokens, token)}
        if channel == "tokens":
            from api.routes.launchpad import _list_tokens_impl

            body = await asyncio.to_thread(_list_tokens_impl, 0, 0, {})
            rows = {}
            for bucket in ("recent_created", "recent_approaching", "recent_graduated"):
                for r in body.get(bucket) or []:
                    rows[(r.get("token") or "").lower()] = r
            self._prev_rows[("tokens", "tokens")] = rows
            return body
        if channel == "positions":
            # only this socket's own wallets. taking the union across every
            # subscriber sent each client the positions of everyone else watching
            # the same token
            addrs = sorted(sub.addresses)
            return {"upserts": await asyncio.to_thread(d.positions_for, token, addrs)}
        return None

    # recompute subscribed tokens whenever the indexer watermark advances
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                tokens = await self.active_tokens()
                if not tokens:
                    continue
                # tokens are pushed concurrently: a serial loop made one pass cost the
                # sum of every token, so the tick started slipping at about five
                # tokens in view. concurrently it costs the slowest one instead
                await asyncio.gather(
                    *(self._push_token_guarded(t) for t in tokens),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # a failure here must never kill the fanout loop
                continue

    # bound how many tokens refresh at once so the connection pool is not starved
    async def _push_token_guarded(self, token: str) -> None:
        async with self._push_sem:
            await self._push_token(token)

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
                "tokens": self._push_tokens_list,
                "token": self._push_token_state,
                "stats": self._push_stats,
                "trades": self._push_trades,
                "holders": self._push_holders,
                "top_traders": self._push_top_traders,
                "dev_tokens": self._push_dev_tokens,
                "positions": self._push_positions,
            }.get(channel)
            if fn is not None:
                await fn(token, watermark)

    # the explorer list: one frame per tick carrying full rows for tokens that
    # entered, per field patches for tokens that changed, and the membership ids.
    # bytes track what actually happened on chain rather than the table size
    async def _push_tokens_list(self, token: str, watermark: int) -> None:
        from api.routes.launchpad import _list_tokens_impl

        body = await asyncio.to_thread(_list_tokens_impl, 0, 0, {})
        rows: dict[str, dict] = {}
        for bucket in ("recent_created", "recent_approaching", "recent_graduated"):
            for r in body.get(bucket) or []:
                rows[(r.get("token") or "").lower()] = r
        prev = self._prev_rows.get(("tokens", "tokens")) or {}
        self._prev_rows[("tokens", "tokens")] = rows

        new = [r for k, r in rows.items() if k not in prev]
        gone = [k for k in prev if k not in rows]
        patches: dict[str, dict] = {}
        for k, r in rows.items():
            p = prev.get(k)
            if p is None or p == r:
                continue
            diff = {f: v for f, v in r.items() if p.get(f) != v}
            if diff:
                patches[k] = diff
        if not new and not gone and not patches:
            return
        env = self._envelope("tokens", "tokens", watermark, "delta")
        payload = {**env, "new": new, "u": patches, "gone": gone, "ids": body.get("ids") or {
            b: [(r.get("token") or "").lower() for r in (body.get(b) or [])]
            for b in ("recent_created", "recent_approaching", "recent_graduated")
        }}
        await self.broadcast("tokens", "tokens", payload)

    # wrap a channel body in the envelope every frame carries
    def _envelope(self, token: str, channel: str, watermark: int, kind: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "token": token,
            "as_of_block": watermark,
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

    # token: the per trade half of the token detail response, so a client can stop
    # polling /token after first load. snapshot because it is ~25 scalars, where a
    # diff would cost more than the object
    async def _push_token_state(self, token: str, watermark: int) -> None:
        from api.ws_data import token_state

        body = await asyncio.to_thread(token_state, token)
        if not body:
            return
        key = (token, "token")
        if self._prev_rows.get(key) == {"all": body}:
            return
        self._prev_rows[key] = {"all": body}
        env = self._envelope(token, "token", watermark, "snapshot")
        await self.broadcast(token, "token", {**env, **body})

    # stats: an aggregate, so the whole object every time
    async def _push_stats(self, token: str, watermark: int) -> None:
        from api.routes.launchpad import token_stats

        body = await asyncio.to_thread(token_stats, token)
        # the watermark guard upstream only asks whether the chain advanced, and monad
        # produces a block roughly every 400ms, so an idle token resent this whole
        # object forever. suppress on the body the way the token channel already does
        # token_stats stamps its own as_of_block/as_of_ts into the body, so the object
        # differs every block even when nothing about the token moved. compare on the
        # substantive fields only, or an idle token resends this forever
        key = (token, "stats")
        material = {k: v for k, v in body.items() if k not in _STATS_VOLATILE}
        if self._prev_rows.get(key) == {"all": material}:
            return
        self._prev_rows[key] = {"all": material}
        env = self._envelope(token, "stats", watermark, "snapshot")
        await self.broadcast(token, "stats", {**env, **body})

    # trades: append only, ids are stable. the diff's removals are an artefact of the
    # 50 row window scrolling, not deletions, and forwarding them told the client to
    # drop trades that still exist. monad has single slot finality so there is no
    # reorg case left to carry
    async def _push_trades(self, token: str, watermark: int) -> None:
        from api.ws_data import recent_trades

        rows = await asyncio.to_thread(recent_trades, token)
        keyed = {r["id"]: r for r in rows}
        added, _evicted = self._diff_rows(token, "trades", keyed)
        if not added:
            return
        env = self._envelope(token, "trades", watermark, "delta")
        await self.broadcast(token, "trades", {**env, "added": added})

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

    # positions are per wallet. one query covers the union of every subscriber's
    # wallets, then each socket is served its own slice, so cost is per token per
    # tick rather than per subscriber per tick
    async def _push_positions(self, token: str, watermark: int) -> None:
        from api.ws_data import positions_for

        async with self.lock:
            targets = [
                s
                for s in self.subscribers
                if s.wants(token, "positions") and s.addresses and (token, "positions") in s.primed
            ]
        if not targets:
            return

        union = sorted({a for s in targets for a in s.addresses})
        rows = await asyncio.to_thread(positions_for, token, union)
        by_addr = {r["address"]: r for r in rows}

        for sub in targets:
            mine = {a: by_addr[a] for a in sub.addresses if a in by_addr}
            key = (token, f"positions:{id(sub)}")
            prev = self._prev_rows.get(key)
            self._prev_rows[key] = mine
            if prev is None:
                upserts, removed = list(mine.values()), []
            else:
                upserts = [v for k, v in mine.items() if prev.get(k) != v]
                removed = [k for k in prev if k not in mine]
            if not upserts and not removed:
                continue
            env = self._envelope(token, "positions", watermark, "delta")
            payload = {**env, "upserts": upserts, "seq": sub.next_seq(token, "positions")}
            if removed:
                payload["removed"] = removed
            await sub.send(payload)


def _watermark() -> int:
    from api.ws_data import indexer_watermark

    return indexer_watermark()


HUB = Hub()


# validate and apply one subscribe frame, returning the reply to send back
async def _apply_subscribe(sub: Subscriber, msg: dict[str, Any]) -> dict[str, Any]:
    token = str(msg.get("token") or "").lower().strip()
    channels = msg.get("channels") or []
    if token != "tokens" and (not token.startswith("0x") or len(token) != 42):
        return {"op": "error", "error": "invalid token address"}
    if not isinstance(channels, list) or not channels:
        return {"op": "error", "error": "channels must be a non-empty list"}

    unknown = [c for c in channels if c not in KNOWN_CHANNELS]
    if unknown:
        return {"op": "error", "error": f"unknown channels: {unknown}", "known": list(KNOWN_CHANNELS)}

    accepted = [c for c in channels if c in IMPLEMENTED_CHANNELS]
    pending = [c for c in channels if c not in IMPLEMENTED_CHANNELS]
    sub.subscriptions.setdefault(token, set()).update(accepted)

    # an addresses array replaces the set rather than adding to it. unioning meant a
    # user who switched wallets kept receiving, and the page kept showing, the
    # position of the wallet they had just left
    if "addresses" in msg:
        addresses = msg.get("addresses") or []
        if isinstance(addresses, list):
            wanted = set()
            for a in addresses:
                a = str(a or "").lower().strip()
                if a.startswith("0x") and len(a) == 42:
                    wanted.add(a)
            if wanted != sub.addresses:
                sub.addresses = wanted
                # the baseline is stale for a different wallet set
                for tok, _ch in list(sub.primed):
                    sub.primed.discard((tok, "positions"))

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
        dropped = sub.subscriptions.pop(token, set())
        for ch in dropped:
            sub.primed.discard((token, ch))
        if "positions" in dropped and not sub.subscriptions:
            sub.addresses.clear()
        return {"op": "unsubscribed", "token": token, "channels": "all"}
    sub.subscriptions[token] -= set(channels)
    for ch in channels:
        sub.primed.discard((token, str(ch)))
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
                reply = await _apply_subscribe(sub, msg)
                await sub.send(reply)
                # baseline immediately rather than waiting for the next change, so a
                # client joining a token others already watch is never left empty
                tok = reply.get("token")
                for ch in (reply.get("channels") or []) if tok else []:
                    # a failed baseline must not drop the socket: the client is still
                    # subscribed and the next change will reach it
                    with contextlib.suppress(Exception):
                        await HUB.send_snapshot(sub, tok, ch)
            elif op == "unsubscribe":
                await sub.send(await _apply_unsubscribe(sub, msg))
            elif op == "query":
                from api.api import _internal_addrs
                from api.routes.launchpad import _search_impl

                f = msg.get("filters") or {}
                try:
                    res = await asyncio.to_thread(
                        _search_impl,
                        str(f.pop("query", "") or ""),
                        str(f.pop("sort", "") or ""),
                        int(f.pop("limit", 50) or 50),
                        int(f.pop("offset", 0) or 0),
                        f,
                        _internal_addrs(),
                    )
                    await sub.send({"op": "query_result", "id": msg.get("id"), **res})
                except Exception:
                    await sub.send({"op": "error", "id": msg.get("id"), "error": "query failed"})
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
