from __future__ import annotations

import importlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from urllib.request import urlopen

from core.ledger.types import TraceResult, TxMeta

DEFAULT_RPC_URL = "https://rpc.monad.xyz"
DEFAULT_MAX_RPS = 20.0
BATCH_SIZE = 50
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 0.5
HTTP_TIMEOUT_SECONDS = 30.0
VALUE_CALL_TYPES = frozenset({"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"})
RETRYABLE_ERROR_CODES = frozenset({-32005, -32603, 429})
RETRYABLE_ERROR_WORDS = ("rate", "limit", "too many", "timeout", "timed out", "busy", "try again")
MEMORY_CACHE_LIMIT = 100_000


class RpcError(Exception):
    pass


class RateLimiter:
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


_default_limiter: RateLimiter | None = None
_default_limiter_lock = threading.Lock()


def shared_limiter() -> RateLimiter:
    global _default_limiter
    with _default_limiter_lock:
        if _default_limiter is None:
            _default_limiter = RateLimiter()
        return _default_limiter


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
    ) -> None:
        self.url = url or os.getenv("RPC_HTTP") or DEFAULT_RPC_URL
        self.limiter = limiter or shared_limiter()
        self.batch_size = max(int(batch_size), 1)
        self.max_attempts = max(int(max_attempts), 1)
        self.timeout = timeout
        self._sleep = sleep

    def _post(self, payload: list[dict]) -> list[dict]:
        body = json.dumps(payload).encode()
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            if attempt:
                self._sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            self.limiter.acquire(len(payload))
            request = urllib.request.Request(
                self.url, data=body, headers={"content-type": "application/json"}, method="POST"
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
                continue
            if isinstance(data, list):
                return data
            last_error = RpcError(f"batch response was not a list: {str(data)[:200]}")
        raise RpcError(f"rpc unavailable after {self.max_attempts} attempts: {last_error}")

    def batch(self, calls: list[tuple[str, list]]) -> list[dict]:
        results: list[dict | None] = [None] * len(calls)
        pending = list(range(len(calls)))
        for attempt in range(self.max_attempts):
            if not pending:
                break
            if attempt:
                self._sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            retry: list[int] = []
            for chunk_start in range(0, len(pending), self.batch_size):
                chunk = pending[chunk_start : chunk_start + self.batch_size]
                payload = [
                    {"jsonrpc": "2.0", "id": rid, "method": calls[idx][0], "params": calls[idx][1]}
                    for rid, idx in enumerate(chunk, start=1)
                ]
                by_id = {item.get("id"): item for item in self._post(payload) if isinstance(item, dict)}
                for rid, idx in enumerate(chunk, start=1):
                    item = by_id.get(rid)
                    if item is None:
                        item = {"error": {"code": -32603, "message": "missing response for request"}}
                    if "error" in item and _is_retryable(item.get("error")):
                        retry.append(idx)
                    results[idx] = item
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
    def __init__(self, cur_factory, rpc_url, rpc, store, memory_limit) -> None:
        self._cur_factory = cur_factory
        self._rpc = rpc or RpcClient(rpc_url)
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

    def note_block(self, block: dict) -> None:
        if not isinstance(block, dict):
            return
        metas: list[TxMeta] = []
        for tx in block.get("transactions") or []:
            if isinstance(tx, dict):
                meta = tx_meta_from_rpc(tx)
                if meta is not None:
                    metas.append(meta)
        self._persist(metas)

    def fetch_blocks(self, numbers: Iterable[int]) -> dict[int, dict]:
        wanted = sorted({int(number) for number in numbers})
        calls = [("eth_getBlockByNumber", [hex(number), True]) for number in wanted]
        blocks: dict[int, dict] = {}
        for number, item in zip(wanted, self._rpc.batch(calls)):
            block = item.get("result") if "error" not in item else None
            if isinstance(block, dict):
                self.note_block(block)
                blocks[number] = block
        return blocks


class TraceStore(_CachedStore):
    def __init__(
        self,
        cur_factory,
        rpc_url: str | None = None,
        rpc: RpcClient | None = None,
        store=None,
        memory_limit: int = MEMORY_CACHE_LIMIT,
    ) -> None:
        super().__init__(cur_factory, rpc_url, rpc, store, memory_limit)

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
