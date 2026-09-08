from __future__ import annotations

import http.client
import importlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from urllib.request import urlopen

from core.ledger.types import TraceResult, TxMeta

DEFAULT_RPC_URL = "https://rpc.monad.xyz"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
DEFAULT_MAX_RPS = 20.0
BATCH_SIZE = 50
MAX_ATTEMPTS = 12
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0
MIN_RPS = 1.0
COOLDOWN_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 30.0
VALUE_CALL_TYPES = frozenset({"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"})
RETRYABLE_ERROR_CODES = frozenset({-32005, -32603, 429})
HISTORY_METHODS = frozenset(
    {"eth_getTransactionByHash", "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_getBlockReceipts"}
)
RETRYABLE_ERROR_WORDS = ("rate", "limit", "too many", "timeout", "timed out", "busy", "try again")
MEMORY_CACHE_LIMIT = 100_000
BLOCK_FETCH_MIN = 2


class RpcError(Exception):
    pass


class RateLimiter:
    """Spaces calls to the node, and yields when the node pushes back.

    The public endpoint caps every client at a few dozen calls a second and counts each item of a batch,
    so a fleet of replays sharing one egress address is throttled as one. A client that keeps its nominal
    rate through a storm of refusals only makes the storm longer; halving on every refusal and earning the
    rate back one call per answered batch settles the fleet at whatever the node actually serves.
    """

    def __init__(
        self,
        max_rps: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        env_rps = os.getenv("RPC_MAX_RPS")
        self.max_rps = float(max_rps if max_rps is not None else (env_rps or DEFAULT_MAX_RPS))
        if self.max_rps <= 0:
            raise ValueError("RPC_MAX_RPS must be positive")
        self.cap = self.max_rps
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self, count: int = 1) -> float:
        with self._lock:
            now = self._clock()
            start = max(now, self._next_slot)
            wait = start - now
            self._next_slot = start + max(count, 1) / self.max_rps
        if wait > 0:
            self._sleep(wait)
        return wait

    def pushed_back(self) -> None:
        with self._lock:
            self.max_rps = max(MIN_RPS, self.max_rps / 2)
            self._next_slot = max(self._next_slot, self._clock() + COOLDOWN_SECONDS)

    def answered(self) -> None:
        with self._lock:
            self.max_rps = min(self.cap, self.max_rps + 1)

    def eta(self) -> float:
        with self._lock:
            return max(0.0, self._next_slot - self._clock())


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def limiter_for(url: str) -> RateLimiter:
    """One limiter per endpoint for the whole process, so every store shares each node's budget."""
    with _limiters_lock:
        limiter = _limiters.get(url)
        if limiter is None:
            limiter = _limiters[url] = RateLimiter()
        return limiter


def shared_limiter() -> RateLimiter:
    return limiter_for(os.getenv("RPC_HTTP") or DEFAULT_RPC_URL)


def pool_urls(url: str | None = None) -> list[str]:
    """The endpoints that share the metadata and receipt traffic: `RPC_HTTP_POOL`, else the base url alone.

    The public QuickNode endpoint caps a client at fifty calls a second and is the only one that serves
    traces, so traces stay on the base url while the far heavier metadata and receipt calls are spread
    over whatever other public nodes answer.
    """
    configured = [u.strip() for u in os.getenv("RPC_HTTP_POOL", "").split(",") if u.strip()]
    return configured or [url or os.getenv("RPC_HTTP") or DEFAULT_RPC_URL]


def _hex_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text in ("", "0x"):
        return 0
    return int(text, 16)


def _addr(value) -> str | None:
    if not value:
        return None
    return str(value).lower()


def _backoff(attempt: int, jitter: Callable[[], float]) -> float:
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1) * (1 + jitter()))


def _is_rate_limit(error: dict | None) -> bool:
    if not isinstance(error, dict):
        return False
    message = str(error.get("message") or "").lower()
    return error.get("code") in (-32007, 429) or "rate" in message or "limit" in message or "too many" in message


def _is_retryable(error: dict | None) -> bool:
    if not isinstance(error, dict):
        return True
    if error.get("code") in RETRYABLE_ERROR_CODES:
        return True
    message = str(error.get("message") or "").lower()
    return any(word in message for word in RETRYABLE_ERROR_WORDS)


class RpcClient:
    def __init__(
        self,
        url: str | None = None,
        limiter: RateLimiter | None = None,
        batch_size: int = BATCH_SIZE,
        max_attempts: int = MAX_ATTEMPTS,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        urls: list[str] | None = None,
    ) -> None:
        self.url = url or os.getenv("RPC_HTTP") or DEFAULT_RPC_URL
        self.urls = list(urls) if urls else [self.url]
        self.limiters = {u: limiter_for(u) for u in self.urls}
        if limiter is not None:
            self.limiters[self.urls[0]] = limiter
        self.limiter = self.limiters[self.urls[0]]
        self.batch_size = max(int(batch_size), 1)
        self.max_attempts = max(int(max_attempts), 1)
        self.timeout = timeout
        self._sleep = sleep
        self._jitter = jitter

    def _post(self, payload: list[dict]) -> tuple[list[dict], str]:
        body = json.dumps(payload).encode()
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            if attempt:
                self._sleep(_backoff(attempt, self._jitter))
            url = min(self.urls, key=lambda u: self.limiters[u].eta())
            limiter = self.limiters[url]
            limiter.acquire(len(payload))
            request = urllib.request.Request(
                url,
                data=body,
                headers={"content-type": "application/json", "user-agent": USER_AGENT},
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    limiter.pushed_back()
                continue
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
                last_error = exc
                continue
            if isinstance(data, list):
                if any(isinstance(item, dict) and _is_rate_limit(item.get("error")) for item in data):
                    limiter.pushed_back()
                else:
                    limiter.answered()
                return data, url
            last_error = RpcError(f"batch response was not a list: {str(data)[:200]}")
        raise RpcError(f"rpc unavailable after {self.max_attempts} attempts: {last_error}")

    def batch(self, calls: list[tuple[str, list]]) -> list[dict]:
        """Answer every call, asking again elsewhere when a node refuses or has forgotten.

        A public node that answers null for a transaction or block that certainly exists simply does
        not hold that history; taking the null as an answer silently dropped half of one replay's
        receipts. Such a node is cooled down and the item asked again, of another node when there is
        one, until the attempts run out.
        """
        results: list[dict | None] = [None] * len(calls)
        pending = list(range(len(calls)))
        for attempt in range(self.max_attempts):
            if not pending:
                break
            if attempt:
                self._sleep(_backoff(attempt, self._jitter))
            retry: list[int] = []
            for chunk_start in range(0, len(pending), self.batch_size):
                chunk = pending[chunk_start : chunk_start + self.batch_size]
                payload = [
                    {"jsonrpc": "2.0", "id": rid, "method": calls[idx][0], "params": calls[idx][1]}
                    for rid, idx in enumerate(chunk, start=1)
                ]
                answered, url = self._post(payload)
                by_id = {item.get("id"): item for item in answered if isinstance(item, dict)}
                forgotten = False
                for rid, idx in enumerate(chunk, start=1):
                    item = by_id.get(rid)
                    if item is None:
                        item = {"error": {"code": -32603, "message": "missing response for request"}}
                    if "error" in item and _is_retryable(item.get("error")):
                        retry.append(idx)
                    elif (
                        "error" not in item
                        and item.get("result") is None
                        and calls[idx][0] in HISTORY_METHODS
                        and len(self.urls) > 1
                    ):
                        retry.append(idx)
                        forgotten = True
                    results[idx] = item
                if forgotten:
                    self.limiters[url].pushed_back()
            pending = retry
        return [item if item is not None else {"error": {"message": "no response"}} for item in results]

    def call(self, method: str, params: list):
        item = self.batch([(method, params)])[0]
        if "error" in item:
            raise RpcError(str(item["error"]))
        return item.get("result")


def tx_meta_from_rpc(tx: dict) -> TxMeta | None:
    txhash = _addr(tx.get("hash"))
    if not txhash or tx.get("blockNumber") is None:
        return None
    calldata = str(tx.get("input") or "0x")
    selector = calldata[:10].lower() if len(calldata) >= 10 else None
    return TxMeta(
        txhash=txhash,
        block_number=_hex_int(tx.get("blockNumber")),
        tx_index=_hex_int(tx.get("transactionIndex")),
        from_addr=_addr(tx.get("from")),
        to_addr=_addr(tx.get("to")),
        value=_hex_int(tx.get("value")),
        selector=selector,
    )


def parse_call_trace(result) -> TraceResult:
    if not isinstance(result, dict) or not result:
        return TraceResult(available=False, transfers=[])
    transfers: list[tuple[str, str, int]] = []

    def walk(frame, top: bool) -> None:
        if not isinstance(frame, dict) or frame.get("error"):
            return
        if not top:
            call_type = str(frame.get("type") or "").upper()
            value = _hex_int(frame.get("value"))
            sender = _addr(frame.get("from"))
            receiver = _addr(frame.get("to"))
            if call_type in VALUE_CALL_TYPES and value > 0 and sender and receiver:
                transfers.append((sender, receiver, value))
        for child in frame.get("calls") or []:
            walk(child, False)

    walk(result, True)
    return TraceResult(available=True, transfers=transfers)


def _load_store():
    try:
        return importlib.import_module("core.ledger.store")
    except ModuleNotFoundError as exc:
        if exc.name != "core.ledger.store":
            raise
        return None


def _unique_lower(txhashes: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for txhash in txhashes:
        key = str(txhash).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


class _CachedStore:
    def __init__(self, cur_factory, rpc_url, rpc, store, memory_limit, pooled: bool = True) -> None:
        self._cur_factory = cur_factory
        self._rpc = rpc or RpcClient(rpc_url, urls=pool_urls(rpc_url) if pooled else None)
        self._store = store if store is not None else _load_store()
        self._memory: dict = {}
        self._memory_limit = memory_limit

    @property
    def rpc(self) -> RpcClient:
        return self._rpc

    def _db_enabled(self) -> bool:
        return self._store is not None and self._cur_factory is not None

    def _remember(self, key, value) -> None:
        if len(self._memory) >= self._memory_limit:
            self._memory.clear()
        self._memory[key] = value


class TxMetaStore(_CachedStore):
    def __init__(
        self,
        cur_factory,
        rpc_url: str | None = None,
        rpc: RpcClient | None = None,
        store=None,
        memory_limit: int = MEMORY_CACHE_LIMIT,
    ) -> None:
        super().__init__(cur_factory, rpc_url, rpc, store, memory_limit)

    def get_many(self, txhashes: list[str]) -> dict[str, TxMeta]:
        wanted = _unique_lower(txhashes)
        out: dict[str, TxMeta] = {}
        missing: list[str] = []
        for txhash in wanted:
            meta = self._memory.get(txhash)
            if meta is None:
                missing.append(txhash)
            else:
                out[txhash] = meta
        if missing and self._db_enabled():
            with self._cur_factory() as cur:
                found = self._store.get_tx_meta(cur, missing)
            for txhash, meta in found.items():
                key = txhash.lower()
                out[key] = meta
                self._remember(key, meta)
            missing = [txhash for txhash in missing if txhash not in out]
        if missing:
            fetched = self._fetch(missing)
            self._persist(fetched)
            for meta in fetched:
                out[meta.txhash] = meta
        return out

    def _fetch(self, txhashes: list[str]) -> list[TxMeta]:
        calls = [("eth_getTransactionByHash", [txhash]) for txhash in txhashes]
        metas: list[TxMeta] = []
        for item in self._rpc.batch(calls):
            tx = item.get("result") if "error" not in item else None
            if isinstance(tx, dict):
                meta = tx_meta_from_rpc(tx)
                if meta is not None:
                    metas.append(meta)
        return metas

    def _persist(self, metas: list[TxMeta]) -> None:
        if not metas:
            return
        for meta in metas:
            self._remember(meta.txhash, meta)
        if self._db_enabled():
            with self._cur_factory() as cur:
                self._store.put_tx_meta(cur, metas)

    def warm(self, hashes_by_block: dict[int, list[str]]) -> None:
        """Fetch the metadata of many blocks' transactions in as few calls as the node charges for.

        Every call in a batch counts against the rate limit, so a block with several wanted transactions
        is fetched whole with one call and only the wanted transactions are kept.
        """
        wanted_by_block: dict[int, list[str]] = {}
        for number, txhashes in hashes_by_block.items():
            missing = [txhash for txhash in _unique_lower(txhashes) if txhash not in self._memory]
            if missing:
                wanted_by_block[int(number)] = missing
        if wanted_by_block and self._db_enabled():
            with self._cur_factory() as cur:
                found = self._store.get_tx_meta(cur, [txh for txhs in wanted_by_block.values() for txh in txhs])
            for txhash, meta in found.items():
                self._remember(txhash.lower(), meta)
            wanted_by_block = {n: [txh for txh in txhs if txh not in found] for n, txhs in wanted_by_block.items()}
        calls: list[tuple[str, list]] = []
        singles: list[str] = []
        for number in sorted(wanted_by_block):
            txhashes = wanted_by_block[number]
            if len(txhashes) >= BLOCK_FETCH_MIN:
                calls.append(("eth_getBlockByNumber", [hex(number), True]))
            else:
                singles.extend(txhashes)
        calls.extend(("eth_getTransactionByHash", [txhash]) for txhash in singles)
        if not calls:
            return
        wanted = {txhash for txhashes in wanted_by_block.values() for txhash in txhashes}
        metas: list[TxMeta] = []
        for (method, _params), item in zip(calls, self._rpc.batch(calls)):
            result = item.get("result") if "error" not in item else None
            if not isinstance(result, dict):
                continue
            txs = (result.get("transactions") or []) if method == "eth_getBlockByNumber" else [result]
            for tx in txs:
                if isinstance(tx, dict) and _addr(tx.get("hash")) in wanted:
                    meta = tx_meta_from_rpc(tx)
                    if meta is not None:
                        metas.append(meta)
        self._persist(metas)


class TraceStore(_CachedStore):
    def __init__(
        self,
        cur_factory,
        rpc_url: str | None = None,
        rpc: RpcClient | None = None,
        store=None,
        memory_limit: int = MEMORY_CACHE_LIMIT,
    ) -> None:
        super().__init__(cur_factory, rpc_url, rpc, store, memory_limit, pooled=False)

    def native_transfers(self, txhash: str) -> TraceResult:
        key = str(txhash).lower()
        cached = self._memory.get(key)
        if cached is not None:
            return cached
        if self._db_enabled():
            with self._cur_factory() as cur:
                stored = self._store.get_trace(cur, key)
            if stored is not None:
                self._remember(key, stored)
                return stored
        try:
            item = self._rpc.batch([("debug_traceTransaction", [key, {"tracer": "callTracer"}])])[0]
        except RpcError:
            return TraceResult(available=False, transfers=[])
        result = parse_call_trace(item.get("result") if "error" not in item else None)
        self._remember(key, result)
        if self._db_enabled():
            with self._cur_factory() as cur:
                self._store.put_trace(cur, key, result)
        return result
