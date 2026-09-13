"""One place that knows how to reach a Monad node, and what to do when the node does not answer.

The primary url is RPC_HTTP. RPC_HTTP_FALLBACKS lists, comma separated, the nodes to try when the primary
cannot be reached, and defaults to the public nodes. A node that fails at the transport level, or answers
with a non-2xx status or a body that is not JSON, is set aside for RPC_FAILOVER_COOLDOWN_SECONDS and then
tried first again, so the primary comes back on its own once it is healthy. A JSON-RPC error body is the
node's answer rather than a failure of the node: it is returned as-is and never causes a failover, since
every node would say the same thing.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx

DEFAULT_RPC_URL = "https://rpc.monad.xyz"
DEFAULT_FALLBACKS = ("https://rpc1.monad.xyz", "https://rpc.monad.xyz")


class RpcUnavailable(RuntimeError):
    pass


def configured_urls() -> list[str]:
    primary = (os.getenv("RPC_HTTP") or DEFAULT_RPC_URL).strip()
    raw = os.getenv("RPC_HTTP_FALLBACKS")
    fallbacks = [u.strip() for u in raw.split(",") if u.strip()] if raw is not None else list(DEFAULT_FALLBACKS)
    urls: list[str] = []
    for url in [primary, *fallbacks]:
        if url and url not in urls:
            urls.append(url)
    return urls


class Endpoints:
    def __init__(self, urls: list[str] | None = None, cooldown: float | None = None) -> None:
        self._urls = list(urls) if urls else configured_urls()
        self._cooldown = (
            float(os.getenv("RPC_FAILOVER_COOLDOWN_SECONDS", "60")) if cooldown is None else float(cooldown)
        )
        self._failed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def urls(self) -> list[str]:
        return list(self._urls)

    @property
    def primary(self) -> str:
        return self._urls[0]

    def ordered(self) -> list[str]:
        """Every node in configured order, with those still cooling down after a failure moved last."""
        now = time.monotonic()
        with self._lock:
            live = [u for u in self._urls if now - self._failed_at.get(u, float("-inf")) >= self._cooldown]
            cooling = [u for u in self._urls if u not in live]
        return live + cooling

    def mark_failed(self, url: str) -> None:
        with self._lock:
            self._failed_at[url] = time.monotonic()

    def mark_ok(self, url: str) -> None:
        with self._lock:
            self._failed_at.pop(url, None)


ENDPOINTS = Endpoints()


def _node_failed(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError, ValueError))


def _note(url: str, exc: BaseException, nxt: str | None) -> None:
    where = f", trying {nxt}" if nxt else ", no node left"
    print(f"[RPC] {url} failed ({exc.__class__.__name__}: {str(exc)[:80]}){where}", flush=True)


def post(payload: Any, timeout: float = 10.0, endpoints: Endpoints | None = None) -> Any:
    """POST a JSON-RPC payload (a call or a batch) to the first node that answers, and return its JSON."""
    endpoints = endpoints or ENDPOINTS
    order = endpoints.ordered()
    last: BaseException | None = None
    for i, url in enumerate(order):
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            if not _node_failed(exc):
                raise
            last = exc
            endpoints.mark_failed(url)
            _note(url, exc, order[i + 1] if i + 1 < len(order) else None)
            continue
        endpoints.mark_ok(url)
        return body
    raise RpcUnavailable(f"no rpc node answered: {last!r}")


async def async_post(client: httpx.AsyncClient, payload: Any, endpoints: Endpoints | None = None, before=None) -> Any:
    """The async twin of post, over a caller's client; `before` is awaited ahead of every attempt."""
    endpoints = endpoints or ENDPOINTS
    order = endpoints.ordered()
    last: BaseException | None = None
    for i, url in enumerate(order):
        if before is not None:
            await before()
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            if not _node_failed(exc):
                raise
            last = exc
            endpoints.mark_failed(url)
            _note(url, exc, order[i + 1] if i + 1 < len(order) else None)
            continue
        endpoints.mark_ok(url)
        return body
    raise RpcUnavailable(f"no rpc node answered: {last!r}")


def call(method: str, params: list, timeout: float = 10.0, endpoints: Endpoints | None = None) -> Any:
    """One JSON-RPC call; a JSON-RPC error body raises RuntimeError with the node's error."""
    body = post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout, endpoints=endpoints)
    if not isinstance(body, dict):
        raise RuntimeError(f"invalid rpc response: {body!r}"[:200])
    if body.get("error") is not None:
        raise RuntimeError(str(body["error"]))
    return body.get("result")
