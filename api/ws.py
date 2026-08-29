from __future__ import annotations

import asyncio
import contextlib
import json
import select
import threading
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

KNOWN_CHANNELS = (
    "token",
    "stats",
    "trades",
    "holders",
    "positions",
    "top_traders",
    "dev_tokens",
    "tokens",
    "user_positions",
    "balances",
    "vaults",
    "user_orders",
    "user_trades",
    "user_history",
)
IMPLEMENTED_CHANNELS = (
    "token",
    "stats",
    "trades",
    "holders",
    "positions",
    "top_traders",
    "dev_tokens",
    "tokens",
    "user_positions",
    "balances",
    "vaults",
    "user_orders",
    "user_trades",
    "user_history",
)

_SNAPSHOT_ORDER = {
    "user_positions": 0,
    "balances": 1,
    "positions": 2,
    "token": 3,
    "stats": 4,
    "trades": 5,
    "vaults": 6,
}

ORDERBOOK_CHANNELS = ("user_orders", "user_trades", "user_history")
ORDERBOOK_HISTORY_LIMIT = 500


def _orderbook_wallet_body(channel: str, wallet: str) -> dict:
    import core.storage as storage

    if channel == "user_orders":
        return {"orders": storage.list_open_orders(wallet)}
    if channel == "user_trades":
        return {"trades": storage.list_exchange_trades(wallet, limit=ORDERBOOK_HISTORY_LIMIT)}
    return {"orders": storage.list_wallet_orders(wallet, limit=ORDERBOOK_HISTORY_LIMIT)}


PSEUDO_TOKENS = ("tokens", "portfolio", "vaults")

SNAPSHOT_CHANNELS = ("token", "stats", "dev_tokens")

FALLBACK_TICK_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.3

IDLE_TIMEOUT_SECONDS = 300

MAX_CONCURRENT_TOKEN_PUSHES = 6

SNAPSHOT_READ_ATTEMPTS = 3

VAULTS_INTERVAL_SECONDS = 5.0

BALANCES_INTERVAL_SECONDS = 3.0

_STATS_VOLATILE = frozenset({"as_of_block", "as_of_ts"})


def _offer_wake(queue: asyncio.Queue) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(True)


def _listen_blocks(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, stop: threading.Event) -> None:
    from core.storage.base import listen_connection

    while not stop.is_set():
        conn = None
        try:
            conn = listen_connection()
            cur = conn.cursor()
            cur.execute("LISTEN crystal_new_block;")
            while not stop.is_set():
                if select.select([conn], [], [], 1.0)[0]:
                    conn.poll()
                    if conn.notifies:
                        conn.notifies.clear()
                        loop.call_soon_threadsafe(_offer_wake, queue)
        except Exception:
            time.sleep(2.0)
        finally:
            with contextlib.suppress(Exception):
                if conn is not None:
                    conn.close()


def _vaults_list_body(addresses: list[str]) -> dict[str, Any]:
    from api.routes.vaults import list_vaults

    user = addresses[0] if addresses else None
    rows: list[dict] = []
    total = 0
    page = 1
    while page <= 20:
        body = list_vaults(
            user=user,
            search=None,
            status="all",
            sort="latest_deposit",
            order="desc",
            page=page,
            limit=50,
            include_snapshot=True,
            snapshot_timeframe="1",
            snapshot_points=48,
        )
        rows.extend(body.get("vaults") or [])
        total = int(body.get("total") or len(rows))
        if not body.get("hasMore"):
            break
        page += 1
    return {"vaults": rows, "total": total}


class Subscriber:
    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket
        self.subscriptions: dict[str, set[str]] = {}
        self.addresses: set[str] = set()
        self.primed: set[tuple[str, str]] = set()
        self._seq: dict[tuple[str, str], int] = {}
        self.last_seen = time.time()
        self.send_lock = asyncio.Lock()

    def next_seq(self, token: str, channel: str) -> int:
        key = (token, channel)
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def tokens(self) -> set[str]:
        return set(self.subscriptions.keys())

    def wants(self, token: str, channel: str) -> bool:
        return channel in self.subscriptions.get(token, ())

    async def send(self, payload: dict[str, Any]) -> bool:
        return await self.send_text(json.dumps(payload))

    async def send_text(self, text: str) -> bool:
        async with self.send_lock:
            try:
                await self.socket.send_text(text)
                return True
            except Exception:
                return False


class Hub:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()
        self.lock = asyncio.Lock()
        self._last_sent: dict[tuple[str, str], int] = {}
        self._prev_rows: dict[tuple[str, str], dict[str, dict]] = {}
        self._task: asyncio.Task | None = None
        self._push_sem = asyncio.Semaphore(MAX_CONCURRENT_TOKEN_PUSHES)
        self._balances_checked: dict[str, float] = {}
        self._vaults_checked = 0.0
        self._tick_calls: dict[tuple, asyncio.Future] = {}
        self._tick_loop: asyncio.AbstractEventLoop | None = None

    async def _tick_call(self, key: tuple, fn, *args):
        loop = asyncio.get_running_loop()
        if self._tick_loop is not loop:
            self._tick_loop = loop
            self._tick_calls = {}
        pending = self._tick_calls.get(key)
        if pending is None:
            pending = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            self._tick_calls[key] = pending
        return await pending

    async def add(self, sub: Subscriber) -> None:
        async with self.lock:
            self.subscribers.add(sub)
            self._ensure_task()

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

    def _stop_task(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    async def active_tokens(self) -> set[str]:
        async with self.lock:
            out: set[str] = set()
            for s in self.subscribers:
                out |= s.tokens()
            return out

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def broadcast(self, token: str, channel: str, payload: dict[str, Any]) -> None:
        async with self.lock:
            targets = [s for s in self.subscribers if s.wants(token, channel) and (token, channel) in s.primed]
        if not targets:
            return
        inner = json.dumps(payload)[1:-1]
        dead = []
        for s in targets:
            seq = s.next_seq(token, channel)
            text = f'{{"seq":{seq},{inner}}}' if inner else f'{{"seq":{seq}}}'
            if not await s.send_text(text):
                dead.append(s)
        for s in dead:
            await self.remove(s)

    async def send_snapshot(self, sub: Subscriber, token: str, channel: str) -> None:
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
            from api.routes.launchpad import _list_tokens_cached

            body = await asyncio.to_thread(_list_tokens_cached, 0, 0)
            rows = {}
            for bucket in ("recent_created", "recent_approaching", "recent_graduated"):
                for r in body.get(bucket) or []:
                    rows[(r.get("token") or "").lower()] = r
            self._prev_rows[("tokens", "tokens")] = rows
            return body
        if channel == "positions":
            addrs = sorted(sub.addresses)
            return {"upserts": await asyncio.to_thread(d.positions_for, token, addrs)}
        if channel == "user_positions":
            addrs = sorted(sub.addresses)
            return {"upserts": await asyncio.to_thread(d.positions_for_wallets, addrs)}
        if channel == "balances":
            from api.spot_data import spot_body

            bodies: dict[str, dict] = {}
            for a in sorted(sub.addresses):
                try:
                    bodies[a] = await asyncio.to_thread(spot_body, a)
                except Exception:
                    continue
            return {"wallets": bodies}
        if channel == "vaults":
            return await asyncio.to_thread(_vaults_list_body, sorted(sub.addresses))
        if channel in ORDERBOOK_CHANNELS:
            from api.routes.orderbook import orderbook_data_is_stale

            if await asyncio.to_thread(orderbook_data_is_stale):
                return None
            bodies: dict[str, dict] = {}
            for a in sorted(sub.addresses):
                try:
                    bodies[a] = await asyncio.to_thread(_orderbook_wallet_body, channel, a)
                except Exception:
                    continue
            return {"wallets": bodies}
        return None

    async def _run(self) -> None:
        wake: asyncio.Queue = asyncio.Queue(maxsize=1)
        stop = threading.Event()
        listener = threading.Thread(target=_listen_blocks, args=(asyncio.get_running_loop(), wake, stop), daemon=True)
        listener.start()
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.get(), timeout=FALLBACK_TICK_SECONDS)
                try:
                    tokens = await self.active_tokens()
                    if not tokens:
                        continue
                    self._tick_calls = {}
                    watermark = await asyncio.to_thread(_watermark)
                    await asyncio.gather(
                        *(self._push_token_guarded(t, watermark) for t in tokens),
                        return_exceptions=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
        finally:
            stop.set()

    async def _push_token_guarded(self, token: str, watermark: int) -> None:
        async with self._push_sem:
            await self._push_token(token, watermark)

    async def _push_token(self, token: str, watermark: int) -> None:
        async with self.lock:
            wanted: set[str] = set()
            for sub in self.subscribers:
                wanted |= sub.subscriptions.get(token, set())

        for channel in wanted:
            key = (token, channel)
            if (
                channel not in ("positions", "user_positions", "balances", "vaults", *ORDERBOOK_CHANNELS)
                and self._last_sent.get(key) == watermark
            ):
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
                "user_positions": self._push_user_positions,
                "balances": self._push_balances,
                "vaults": self._push_vaults,
            }.get(channel)
            if fn is not None:
                await fn(token, watermark)
            elif channel in ORDERBOOK_CHANNELS:
                await self._push_orderbook_channel(token, watermark, channel)

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
        payload = {
            **env,
            "new": new,
            "u": patches,
            "gone": gone,
            "ids": body.get("ids")
            or {
                b: [(r.get("token") or "").lower() for r in (body.get(b) or [])]
                for b in ("recent_created", "recent_approaching", "recent_graduated")
            },
        }
        await self.broadcast("tokens", "tokens", payload)

    def _envelope(self, token: str, channel: str, watermark: int, kind: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "token": token,
            "as_of_block": watermark,
            "kind": kind,
        }

    def _diff_rows(self, token: str, channel: str, rows: dict[str, dict]) -> tuple[list[dict], list[str]]:
        key = (token, channel)
        prev = self._prev_rows.get(key)
        self._prev_rows[key] = rows
        if prev is None:
            return list(rows.values()), []
        upserts = [v for k, v in rows.items() if prev.get(k) != v]
        removed = [k for k in prev if k not in rows]
        return upserts, removed

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

    async def _push_stats(self, token: str, watermark: int) -> None:
        from api.routes.launchpad import token_stats

        body = await asyncio.to_thread(token_stats, token)
        key = (token, "stats")
        material = {k: v for k, v in body.items() if k not in _STATS_VOLATILE}
        if self._prev_rows.get(key) == {"all": material}:
            return
        self._prev_rows[key] = {"all": material}
        env = self._envelope(token, "stats", watermark, "snapshot")
        await self.broadcast(token, "stats", {**env, **body})

    async def _push_trades(self, token: str, watermark: int) -> None:
        from api.ws_data import recent_trades

        rows = await asyncio.to_thread(recent_trades, token)
        keyed = {r["id"]: r for r in rows}
        added, _evicted = self._diff_rows(token, "trades", keyed)
        if not added:
            return
        env = self._envelope(token, "trades", watermark, "delta")
        await self.broadcast(token, "trades", {**env, "added": added})

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

    async def _push_dev_tokens(self, token: str, watermark: int) -> None:
        from api.ws_data import dev_tokens

        rows = await asyncio.to_thread(dev_tokens, token)
        key = (token, "dev_tokens")
        if self._prev_rows.get(key) == {"all": {"rows": rows}}:
            return
        self._prev_rows[key] = {"all": {"rows": rows}}
        env = self._envelope(token, "dev_tokens", watermark, "snapshot")
        await self.broadcast(token, "dev_tokens", {**env, "devTokens": rows})

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

    async def _push_user_positions(self, token: str, watermark: int) -> None:
        from api.ws_data import positions_for_wallets

        async with self.lock:
            targets = [
                s
                for s in self.subscribers
                if s.wants(token, "user_positions") and s.addresses and (token, "user_positions") in s.primed
            ]
        if not targets:
            return

        union = sorted({a for s in targets for a in s.addresses})
        rows = await asyncio.to_thread(positions_for_wallets, union)
        by_key: dict[str, dict] = {f"{r['address']}:{r['token']}": r for r in rows}

        for sub in targets:
            mine = {k: v for k, v in by_key.items() if v["address"] in sub.addresses}
            key = (token, f"user_positions:{id(sub)}")
            prev = self._prev_rows.get(key)
            self._prev_rows[key] = mine
            if prev is None:
                upserts, removed = list(mine.values()), []
            else:
                upserts = [v for k, v in mine.items() if prev.get(k) != v]
                removed = [k for k in prev if k not in mine]
            if not upserts and not removed:
                continue
            env = self._envelope(token, "user_positions", watermark, "delta")
            payload = {**env, "upserts": upserts, "seq": sub.next_seq(token, "user_positions")}
            if removed:
                payload["removed"] = removed
            await sub.send(payload)

    async def _push_balances(self, token: str, watermark: int) -> None:
        from api.spot_data import spot_body

        async with self.lock:
            targets = [
                s
                for s in self.subscribers
                if s.wants(token, "balances") and s.addresses and (token, "balances") in s.primed
            ]
        if not targets:
            return

        now = time.time()
        union = sorted({a for s in targets for a in s.addresses})
        changed: dict[str, dict] = {}
        for a in union:
            if now - self._balances_checked.get(a, 0.0) < BALANCES_INTERVAL_SECONDS:
                continue
            self._balances_checked[a] = now
            try:
                body = await asyncio.to_thread(spot_body, a)
            except Exception:
                continue
            material = {k: v for k, v in body.items() if k != "balance_block"}
            key = (token, f"balances:{a}")
            if self._prev_rows.get(key) == {"all": material}:
                continue
            self._prev_rows[key] = {"all": material}
            changed[a] = body
        if not changed:
            return
        for sub in targets:
            mine = {a: b for a, b in changed.items() if a in sub.addresses}
            if not mine:
                continue
            env = self._envelope(token, "balances", watermark, "delta")
            await sub.send({**env, "wallets": mine, "seq": sub.next_seq(token, "balances")})

    async def _push_orderbook_channel(self, token: str, watermark: int, channel: str) -> None:
        from api.routes.orderbook import orderbook_data_is_stale

        if await self._tick_call(("orderbook_stale",), orderbook_data_is_stale):
            return
        async with self.lock:
            targets = [
                s for s in self.subscribers if s.wants(token, channel) and s.addresses and (token, channel) in s.primed
            ]
        if not targets:
            return

        union = sorted({a for s in targets for a in s.addresses})
        changed: dict[str, dict] = {}
        for a in union:
            try:
                body = await self._tick_call(("orderbook_body", channel, a), _orderbook_wallet_body, channel, a)
            except Exception:
                continue
            key = (token, f"{channel}:{a}")
            if self._prev_rows.get(key) == {"all": body}:
                continue
            self._prev_rows[key] = {"all": body}
            changed[a] = body
        if not changed:
            return
        for sub in targets:
            mine = {a: b for a, b in changed.items() if a in sub.addresses}
            if not mine:
                continue
            env = self._envelope(token, channel, watermark, "delta")
            await sub.send({**env, "wallets": mine, "seq": sub.next_seq(token, channel)})

    async def _push_vaults(self, token: str, watermark: int) -> None:
        async with self.lock:
            targets = [s for s in self.subscribers if s.wants(token, "vaults") and (token, "vaults") in s.primed]
        if not targets:
            return
        now = time.time()
        if now - self._vaults_checked < VAULTS_INTERVAL_SECONDS:
            return
        self._vaults_checked = now

        bodies: dict[str, dict | None] = {}
        for sub in targets:
            ukey = sorted(sub.addresses)[0] if sub.addresses else ""
            if ukey not in bodies:
                try:
                    bodies[ukey] = await asyncio.to_thread(_vaults_list_body, sorted(sub.addresses))
                except Exception:
                    bodies[ukey] = None
            body = bodies.get(ukey)
            if body is None:
                continue
            key = (token, f"vaults:{id(sub)}")
            if self._prev_rows.get(key) == {"all": body}:
                continue
            self._prev_rows[key] = {"all": body}
            env = self._envelope(token, "vaults", watermark, "snapshot")
            await sub.send({**env, **body, "seq": sub.next_seq(token, "vaults")})


def _watermark() -> int:
    from api.ws_data import indexer_watermark

    return indexer_watermark()


HUB = Hub()


async def _apply_subscribe(sub: Subscriber, msg: dict[str, Any]) -> dict[str, Any]:
    token = str(msg.get("token") or "").lower().strip()
    channels = msg.get("channels") or []
    if token not in PSEUDO_TOKENS and (not token.startswith("0x") or len(token) != 42):
        return {"op": "error", "error": "invalid token address"}
    if not isinstance(channels, list) or not channels:
        return {"op": "error", "error": "channels must be a non-empty list"}

    unknown = [c for c in channels if c not in KNOWN_CHANNELS]
    if unknown:
        return {"op": "error", "error": f"unknown channels: {unknown}", "known": list(KNOWN_CHANNELS)}

    accepted = [c for c in channels if c in IMPLEMENTED_CHANNELS]
    pending = [c for c in channels if c not in IMPLEMENTED_CHANNELS]
    sub.subscriptions.setdefault(token, set()).update(accepted)

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
                for tok, _ch in list(sub.primed):
                    sub.primed.discard((tok, "positions"))
                    sub.primed.discard((tok, "user_positions"))
                    sub.primed.discard((tok, "balances"))
                    for ch in ORDERBOOK_CHANNELS:
                        sub.primed.discard((tok, ch))

    reply = {
        "op": "subscribed",
        "token": token,
        "channels": accepted,
        "not_yet_implemented": pending,
    }
    if any(c in accepted for c in ("positions", "user_positions", "balances", *ORDERBOOK_CHANNELS)):
        reply["addresses"] = sorted(sub.addresses)
        if not sub.addresses:
            reply["warning"] = "positions needs an addresses array, none were accepted"
    return reply


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
                tok = reply.get("token")
                chans = (reply.get("channels") or []) if tok else []
                for ch in sorted(chans, key=lambda c: _SNAPSHOT_ORDER.get(c, 99)):
                    with contextlib.suppress(Exception):
                        await HUB.send_snapshot(sub, tok, ch)
            elif op == "unsubscribe":
                await sub.send(await _apply_unsubscribe(sub, msg))
            elif op == "query":
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
