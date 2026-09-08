import json
import os
import sys
import urllib.error
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ledger import txmeta
from core.ledger.txmeta import (
    RateLimiter,
    RpcClient,
    RpcError,
    TraceStore,
    TxMetaStore,
    parse_call_trace,
    tx_meta_from_rpc,
)
from core.ledger.types import TraceResult, TxMeta

FIXTURE_TX = "0xcb3461b56e39f9741e974df5f4a7d0e487d9c042371cd91501a3e6c0130dca5b"
FIXTURE_FROM = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
SETTLER = "0xc2d3689cf6ce2859a3ffbc8fe09ab4c8623766b8"
ROUTER = "0x1ab7ea18e4b2a1f0d8c4c2a5c3b1c5c0c9e7a6b5"
CORE = "0x6eb2af5fc575689053ac9b413220cabfd01a2f9a"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
FEE_SINK = "0x565e9c68cbd1b6b0f9a7f2d5c4e3b2a1f0e9d8c7"
TOKEN = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
ALLOWANCE_HOLDER = "0x0000000000001ff3684f28c67538d4d072c22734"

TX_A = "0x" + "aa" * 32
TX_B = "0x" + "bb" * 32
TX_C = "0x" + "cc" * 32


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    def __init__(self, handler, failures: int = 0, reorder: bool = False) -> None:
        self.handler = handler
        self.failures = failures
        self.reorder = reorder
        self.payloads: list = []

    def __call__(self, request, timeout=None):
        payload = json.loads(request.data)
        self.payloads.append(payload)
        if self.failures > 0:
            self.failures -= 1
            raise urllib.error.URLError("connection reset")
        items = [self.handler(entry) for entry in payload]
        if self.reorder:
            items = list(reversed(items))
        return FakeResponse(json.dumps(items).encode())


def rpc_tx(txhash: str, sender: str = FIXTURE_FROM, to: str | None = ALLOWANCE_HOLDER, value: int = 0) -> dict:
    return {
        "hash": txhash,
        "blockNumber": "0x60d7024",
        "transactionIndex": "0x3",
        "from": sender.upper(),
        "to": to,
        "value": hex(value),
        "input": "0x2213bc0b" + "00" * 64,
    }


def tx_handler(txs: dict[str, dict]):
    def handle(entry: dict) -> dict:
        assert entry["method"] == "eth_getTransactionByHash"
        return {"jsonrpc": "2.0", "id": entry["id"], "result": txs.get(entry["params"][0])}

    return handle


class FakeStore:
    def __init__(self) -> None:
        self.metas: dict[str, TxMeta] = {}
        self.traces: dict[str, TraceResult] = {}
        self.meta_reads: list[list[str]] = []
        self.meta_writes: list[list[TxMeta]] = []
        self.trace_reads: list[str] = []
        self.trace_writes: list[tuple[str, TraceResult]] = []

    def get_tx_meta(self, cur, txhashes):
        self.meta_reads.append(list(txhashes))
        return {h: self.metas[h] for h in txhashes if h in self.metas}

    def put_tx_meta(self, cur, metas):
        self.meta_writes.append(list(metas))
        for meta in metas:
            self.metas[meta.txhash] = meta

    def get_trace(self, cur, txhash):
        self.trace_reads.append(txhash)
        return self.traces.get(txhash)

    def put_trace(self, cur, txhash, result):
        self.trace_writes.append((txhash, result))
        self.traces[txhash] = result


@contextmanager
def fake_cur_factory():
    yield object()


def no_sleep(_seconds: float) -> None:
    return None


def make_client(fake: FakeUrlopen, monkeypatch, **kwargs) -> RpcClient:
    monkeypatch.setattr(txmeta, "urlopen", fake)
    limiter = RateLimiter(max_rps=1_000_000, sleep=no_sleep)
    kwargs.setdefault("sleep", no_sleep)
    return RpcClient("http://rpc.test", limiter=limiter, **kwargs)


def test_get_many_sends_one_batched_post_and_persists(monkeypatch):
    fake = FakeUrlopen(tx_handler({TX_A: rpc_tx(TX_A, value=5), TX_B: rpc_tx(TX_B, to=None)}))
    store = FakeStore()
    metas = TxMetaStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    out = metas.get_many([TX_A, TX_B.upper()])

    assert len(fake.payloads) == 1
    assert [entry["method"] for entry in fake.payloads[0]] == ["eth_getTransactionByHash"] * 2
    assert [entry["params"] for entry in fake.payloads[0]] == [[TX_A], [TX_B]]
    assert out[TX_A] == TxMeta(
        txhash=TX_A,
        block_number=0x60D7024,
        tx_index=3,
        from_addr=FIXTURE_FROM,
        to_addr=ALLOWANCE_HOLDER,
        value=5,
        selector="0x2213bc0b",
    )
    assert out[TX_B].to_addr is None
    assert store.meta_reads == [[TX_A, TX_B]]
    assert [m.txhash for m in store.meta_writes[0]] == [TX_A, TX_B]


def test_get_many_reads_the_db_cache_before_the_rpc(monkeypatch):
    fake = FakeUrlopen(tx_handler({TX_B: rpc_tx(TX_B)}))
    store = FakeStore()
    cached = tx_meta_from_rpc(rpc_tx(TX_A, value=7))
    store.metas[TX_A] = cached
    metas = TxMetaStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    out = metas.get_many([TX_A, TX_B])

    assert out[TX_A] is cached
    assert out[TX_B].txhash == TX_B
    assert [entry["params"] for entry in fake.payloads[0]] == [[TX_B]]


def test_get_many_serves_repeat_lookups_from_memory(monkeypatch):
    fake = FakeUrlopen(tx_handler({TX_A: rpc_tx(TX_A)}))
    store = FakeStore()
    metas = TxMetaStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    first = metas.get_many([TX_A])
    second = metas.get_many([TX_A])

    assert first == second
    assert len(fake.payloads) == 1
    assert len(store.meta_reads) == 1


def test_get_many_matches_responses_by_id_when_reordered(monkeypatch):
    txs = {TX_A: rpc_tx(TX_A, value=1), TX_B: rpc_tx(TX_B, value=2), TX_C: rpc_tx(TX_C, value=3)}
    fake = FakeUrlopen(tx_handler(txs), reorder=True)
    metas = TxMetaStore(None, rpc=make_client(fake, monkeypatch), store=None)

    out = metas.get_many([TX_A, TX_B, TX_C])

    assert {h: m.value for h, m in out.items()} == {TX_A: 1, TX_B: 2, TX_C: 3}


def test_get_many_omits_unknown_hashes_and_error_items(monkeypatch):
    def handle(entry):
        txhash = entry["params"][0]
        if txhash == TX_C:
            return {"jsonrpc": "2.0", "id": entry["id"], "error": {"code": -32000, "message": "not found"}}
        return {"jsonrpc": "2.0", "id": entry["id"], "result": rpc_tx(TX_A) if txhash == TX_A else None}

    fake = FakeUrlopen(handle)
    metas = TxMetaStore(None, rpc=make_client(fake, monkeypatch), store=None)

    out = metas.get_many([TX_A, TX_B, TX_C])

    assert set(out) == {TX_A}
    assert len(fake.payloads) == 1


def test_get_many_chunks_large_requests(monkeypatch):
    hashes = ["0x" + f"{i:064x}" for i in range(120)]
    fake = FakeUrlopen(tx_handler({h: rpc_tx(h) for h in hashes}))
    client = make_client(fake, monkeypatch, batch_size=50)
    metas = TxMetaStore(None, rpc=client, store=None)

    out = metas.get_many(hashes)

    assert len(out) == 120
    assert [len(p) for p in fake.payloads] == [50, 50, 20]


def test_get_many_retries_transport_errors_with_backoff(monkeypatch):
    fake = FakeUrlopen(tx_handler({TX_A: rpc_tx(TX_A)}), failures=2)
    sleeps: list[float] = []
    client = make_client(fake, monkeypatch, sleep=sleeps.append, jitter=lambda: 0.0)
    metas = TxMetaStore(None, rpc=client, store=None)

    out = metas.get_many([TX_A])

    assert TX_A in out
    assert len(fake.payloads) == 3
    assert sleeps == [0.5, 1.0]


def test_get_many_raises_after_exhausting_attempts(monkeypatch):
    fake = FakeUrlopen(tx_handler({}), failures=99)
    client = make_client(fake, monkeypatch, max_attempts=3)
    metas = TxMetaStore(None, rpc=client, store=None)

    with pytest.raises(RpcError):
        metas.get_many([TX_A])
    assert len(fake.payloads) == 3


def test_batch_retries_only_rate_limited_items(monkeypatch):
    seen: dict[str, int] = {}

    def handle(entry):
        txhash = entry["params"][0]
        seen[txhash] = seen.get(txhash, 0) + 1
        if txhash == TX_B and seen[txhash] == 1:
            return {"jsonrpc": "2.0", "id": entry["id"], "error": {"code": -32005, "message": "rate limit exceeded"}}
        return {"jsonrpc": "2.0", "id": entry["id"], "result": rpc_tx(txhash)}

    fake = FakeUrlopen(handle)
    metas = TxMetaStore(None, rpc=make_client(fake, monkeypatch), store=None)

    out = metas.get_many([TX_A, TX_B])

    assert set(out) == {TX_A, TX_B}
    assert [[e["params"][0] for e in p] for p in fake.payloads] == [[TX_A, TX_B], [TX_B]]


def test_tx_meta_from_rpc_skips_pending_and_short_input():
    assert tx_meta_from_rpc({"hash": TX_A, "blockNumber": None}) is None
    meta = tx_meta_from_rpc(
        {"hash": TX_A, "blockNumber": "0x1", "transactionIndex": "0x0", "from": FIXTURE_FROM, "input": "0x"}
    )
    assert meta.selector is None
    assert meta.value == 0
    assert meta.to_addr is None


def test_rate_limiter_spaces_requests_and_charges_per_batch_item():
    now = [100.0]
    sleeps: list[float] = []
    limiter = RateLimiter(max_rps=2, clock=lambda: now[0], sleep=sleeps.append)

    assert limiter.acquire() == 0
    assert limiter.acquire() == pytest.approx(0.5)
    now[0] += 0.5
    assert limiter.acquire(10) == pytest.approx(0.5)
    assert limiter.acquire() == pytest.approx(5.5)
    assert sleeps == pytest.approx([0.5, 0.5, 5.5])


def test_rate_limiter_reads_rpc_max_rps_from_env(monkeypatch):
    monkeypatch.setenv("RPC_MAX_RPS", "5")
    assert RateLimiter().max_rps == 5
    monkeypatch.delenv("RPC_MAX_RPS")
    assert RateLimiter().max_rps == 20


def test_rpc_client_counts_batch_items_against_the_budget(monkeypatch):
    acquired: list[int] = []

    class Limiter:
        def acquire(self, count=1):
            acquired.append(count)
            return 0.0

        def answered(self):
            return None

        def pushed_back(self):
            return None

        def eta(self):
            return 0.0

    fake = FakeUrlopen(tx_handler({TX_A: rpc_tx(TX_A), TX_B: rpc_tx(TX_B)}))
    monkeypatch.setattr(txmeta, "urlopen", fake)
    client = RpcClient("http://rpc.test", limiter=Limiter(), sleep=no_sleep)

    client.batch([("eth_getTransactionByHash", [TX_A]), ("eth_getTransactionByHash", [TX_B])])

    assert acquired == [2]


SETTLER_SELL_TRACE = {
    "type": "CALL",
    "from": FIXTURE_FROM,
    "to": ALLOWANCE_HOLDER,
    "value": "0x0",
    "calls": [
        {"type": "STATICCALL", "from": ALLOWANCE_HOLDER, "to": SETTLER},
        {
            "type": "CALL",
            "from": ALLOWANCE_HOLDER,
            "to": SETTLER,
            "value": "0x0",
            "calls": [
                {
                    "type": "CALL",
                    "from": SETTLER,
                    "to": ROUTER,
                    "value": "0x0",
                    "calls": [
                        {"type": "STATICCALL", "from": ROUTER, "to": TOKEN},
                        {
                            "type": "CALL",
                            "from": ROUTER,
                            "to": CORE,
                            "value": "0x0",
                            "calls": [
                                {
                                    "type": "DELEGATECALL",
                                    "from": CORE,
                                    "to": "0x664fdc46",
                                    "value": "0x179c902d8522a53d116",
                                },
                                {
                                    "type": "CALL",
                                    "from": CORE,
                                    "to": WMON,
                                    "value": "0x0",
                                    "calls": [
                                        {"type": "CALL", "from": WMON, "to": CORE, "value": "0x179c902d8522a53d116"},
                                    ],
                                },
                                {"type": "CALL", "from": CORE, "to": ROUTER, "value": "0x179c902d8522a53d116"},
                            ],
                        },
                        {"type": "CALL", "from": ROUTER, "to": SETTLER, "value": "0x179c902d8522a53d116"},
                    ],
                },
                {"type": "CALL", "from": SETTLER, "to": FEE_SINK, "value": "0x3c7214ef694e73cf8"},
                {"type": "CALL", "from": SETTLER, "to": FIXTURE_FROM.upper(), "value": "0x17601e1895b956c941e"},
            ],
        },
    ],
}


def test_parse_call_trace_collects_nested_native_transfers_in_call_order():
    result = parse_call_trace(SETTLER_SELL_TRACE)

    gross = 0x179C902D8522A53D116
    assert result.available is True
    assert result.transfers == [
        (WMON, CORE, gross),
        (CORE, ROUTER, gross),
        (ROUTER, SETTLER, gross),
        (SETTLER, FEE_SINK, 0x3C7214EF694E73CF8),
        (SETTLER, FIXTURE_FROM, 0x17601E1895B956C941E),
    ]


def test_parse_call_trace_excludes_top_level_value_and_reverted_subtrees():
    trace = {
        "type": "CALL",
        "from": FIXTURE_FROM,
        "to": CORE,
        "value": "0x10",
        "calls": [
            {
                "type": "CALL",
                "from": CORE,
                "to": ROUTER,
                "value": "0x5",
                "error": "execution reverted",
                "calls": [{"type": "CALL", "from": ROUTER, "to": SETTLER, "value": "0x5"}],
            },
            {"type": "CREATE", "from": CORE, "to": FEE_SINK, "value": "0x7"},
            {"type": "SELFDESTRUCT", "from": FEE_SINK, "to": FIXTURE_FROM, "value": "0x7"},
            {"type": "CALLCODE", "from": CORE, "to": ROUTER, "value": "0x9"},
        ],
    }

    result = parse_call_trace(trace)

    assert result.transfers == [(CORE, FEE_SINK, 7), (FEE_SINK, FIXTURE_FROM, 7)]


def test_parse_call_trace_of_a_reverted_transaction_is_available_but_empty():
    result = parse_call_trace(
        {"type": "CALL", "from": FIXTURE_FROM, "to": CORE, "value": "0x1", "error": "execution reverted"}
    )
    assert result == TraceResult(available=True, transfers=[])


def test_parse_call_trace_of_empty_result_is_unavailable():
    assert parse_call_trace(None) == TraceResult(available=False, transfers=[])
    assert parse_call_trace({}) == TraceResult(available=False, transfers=[])


def trace_handler(result_by_hash: dict):
    def handle(entry):
        assert entry["method"] == "debug_traceTransaction"
        assert entry["params"][1] == {"tracer": "callTracer"}
        outcome = result_by_hash.get(entry["params"][0])
        if isinstance(outcome, dict) and "error" in outcome:
            return {"jsonrpc": "2.0", "id": entry["id"], "error": outcome["error"]}
        return {"jsonrpc": "2.0", "id": entry["id"], "result": outcome}

    return handle


def test_trace_store_fetches_parses_and_persists(monkeypatch):
    fake = FakeUrlopen(trace_handler({FIXTURE_TX: SETTLER_SELL_TRACE}))
    store = FakeStore()
    traces = TraceStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    result = traces.native_transfers(FIXTURE_TX.upper())
    again = traces.native_transfers(FIXTURE_TX)

    assert result.available is True
    assert result.transfers[-1] == (SETTLER, FIXTURE_FROM, 0x17601E1895B956C941E)
    assert again is result
    assert store.trace_writes == [(FIXTURE_TX, result)]
    assert store.trace_reads == [FIXTURE_TX]
    assert len(fake.payloads) == 1


def test_trace_store_serves_from_the_db_cache(monkeypatch):
    fake = FakeUrlopen(trace_handler({}))
    store = FakeStore()
    stored = TraceResult(available=True, transfers=[(CORE, FIXTURE_FROM, 1)])
    store.traces[FIXTURE_TX] = stored
    traces = TraceStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    assert traces.native_transfers(FIXTURE_TX) is stored
    assert fake.payloads == []


def test_trace_store_marks_node_errors_unavailable_and_persists_that(monkeypatch):
    fake = FakeUrlopen(
        trace_handler({FIXTURE_TX: {"error": {"code": -32000, "message": "historical state not available"}}})
    )
    store = FakeStore()
    traces = TraceStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    result = traces.native_transfers(FIXTURE_TX)

    assert result == TraceResult(available=False, transfers=[])
    assert store.trace_writes == [(FIXTURE_TX, result)]
    assert len(fake.payloads) == 1


def test_trace_store_marks_empty_results_unavailable(monkeypatch):
    fake = FakeUrlopen(trace_handler({FIXTURE_TX: None}))
    store = FakeStore()
    traces = TraceStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    assert traces.native_transfers(FIXTURE_TX).available is False
    assert store.traces[FIXTURE_TX].available is False


def test_trace_store_does_not_persist_transport_failures(monkeypatch):
    fake = FakeUrlopen(trace_handler({}), failures=99)
    store = FakeStore()
    traces = TraceStore(fake_cur_factory, rpc=make_client(fake, monkeypatch, max_attempts=2), store=store)

    result = traces.native_transfers(FIXTURE_TX)

    assert result.available is False
    assert store.trace_writes == []
    assert len(fake.payloads) == 2


def test_stores_work_without_a_db_cache(monkeypatch):
    fake = FakeUrlopen(tx_handler({TX_A: rpc_tx(TX_A)}))
    metas = TxMetaStore(None, rpc=make_client(fake, monkeypatch), store=None)
    assert metas.get_many([TX_A])[TX_A].from_addr == FIXTURE_FROM


@pytest.mark.skipif(os.getenv("LEDGER_LIVE_RPC") != "1", reason="set LEDGER_LIVE_RPC=1 to hit rpc.monad.xyz")
def test_live_fixture_settler_sell_metadata():
    metas = TxMetaStore(None, store=None)
    out = metas.get_many([FIXTURE_TX])
    meta = out[FIXTURE_TX]
    assert meta.from_addr == FIXTURE_FROM
    assert meta.block_number == 0x60D7024
    assert meta.tx_index == 3
    assert meta.value == 0
    assert meta.selector == "0x2213bc0b"


def block_and_tx_handler(blocks: dict[int, list[dict]], txs: dict[str, dict]):
    def handle(entry: dict) -> dict:
        if entry["method"] == "eth_getBlockByNumber":
            number, full = entry["params"]
            assert full is True
            block = {"number": number, "transactions": blocks[int(number, 16)]}
            return {"jsonrpc": "2.0", "id": entry["id"], "result": block}
        assert entry["method"] == "eth_getTransactionByHash"
        return {"jsonrpc": "2.0", "id": entry["id"], "result": txs.get(entry["params"][0])}

    return handle


def test_warm_fetches_one_block_for_several_wanted_transactions_and_keeps_only_them(monkeypatch):
    other = "0x" + "dd" * 32
    blocks = {100: [rpc_tx(TX_A, value=1), rpc_tx(other), rpc_tx(TX_B, value=2)]}
    fake = FakeUrlopen(block_and_tx_handler(blocks, {TX_C: rpc_tx(TX_C, value=3)}))
    store = FakeStore()
    metas = TxMetaStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    metas.warm({100: [TX_A, TX_B], 101: [TX_C]})

    assert [(e["method"], e["params"][0]) for e in fake.payloads[0]] == [
        ("eth_getBlockByNumber", "0x64"),
        ("eth_getTransactionByHash", TX_C),
    ]
    assert sorted(store.metas) == sorted([TX_A, TX_B, TX_C])
    out = metas.get_many([TX_A, TX_B, TX_C, other])
    assert (out[TX_A].value, out[TX_B].value, out[TX_C].value) == (1, 2, 3)
    assert len(fake.payloads) == 2 and [e["params"] for e in fake.payloads[1]] == [[other]]


def test_warm_counts_only_uncached_transactions_toward_a_block_fetch(monkeypatch):
    fake = FakeUrlopen(block_and_tx_handler({}, {TX_B: rpc_tx(TX_B)}))
    store = FakeStore()
    store.metas[TX_A] = TxMeta(
        txhash=TX_A, block_number=100, tx_index=0, from_addr=FIXTURE_FROM, to_addr=None, value=0, selector=None
    )
    metas = TxMetaStore(fake_cur_factory, rpc=make_client(fake, monkeypatch), store=store)

    metas.warm({100: [TX_A, TX_B]})

    assert [(e["method"], e["params"][0]) for e in fake.payloads[0]] == [("eth_getTransactionByHash", TX_B)]
    assert fake.payloads[1:] == []


def test_rate_limiter_yields_when_the_node_pushes_back_and_earns_its_rate_back():
    clock = [0.0]
    limiter = RateLimiter(max_rps=40, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))

    limiter.pushed_back()
    assert limiter.max_rps == 20
    limiter.pushed_back()
    limiter.pushed_back()
    limiter.pushed_back()
    limiter.pushed_back()
    limiter.pushed_back()
    assert limiter.max_rps == 1
    for _ in range(100):
        limiter.answered()
    assert limiter.max_rps == 40


def test_rpc_client_keeps_retrying_a_rate_limited_batch_with_bounded_backoff(monkeypatch):
    calls = {"n": 0}

    def handle(entry):
        calls["n"] += 1
        if calls["n"] <= 8:
            return {
                "jsonrpc": "2.0",
                "id": entry["id"],
                "error": {"code": -32007, "message": "50/second request limit reached"},
            }
        return {"jsonrpc": "2.0", "id": entry["id"], "result": rpc_tx(TX_A, value=4)}

    fake = FakeUrlopen(handle)
    naps: list[float] = []
    monkeypatch.setattr(txmeta, "urlopen", fake)
    limiter = RateLimiter(max_rps=40, sleep=no_sleep)
    client = RpcClient("http://rpc.test", limiter=limiter, sleep=naps.append)

    out = client.batch([("eth_getTransactionByHash", [TX_A])])

    assert out[0]["result"]["value"] == hex(4)
    assert len(fake.payloads) == 9
    assert len(naps) == 8 and max(naps) <= txmeta.BACKOFF_CAP_SECONDS and naps[-1] > naps[0]
    assert limiter.max_rps < 40


def test_rpc_client_gives_up_on_a_persistent_rate_limit_only_after_many_attempts(monkeypatch):
    def handle(entry):
        return {"jsonrpc": "2.0", "id": entry["id"], "error": {"code": 429, "message": "too many requests"}}

    fake = FakeUrlopen(handle)
    monkeypatch.setattr(txmeta, "urlopen", fake)
    client = RpcClient("http://rpc.test", limiter=RateLimiter(max_rps=40, sleep=no_sleep), sleep=no_sleep)

    out = client.batch([("eth_getTransactionByHash", [TX_A])])

    assert "error" in out[0]
    assert len(fake.payloads) == txmeta.MAX_ATTEMPTS >= 12


def test_pool_urls_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RPC_HTTP_POOL", "https://a.test, https://b.test")
    assert txmeta.pool_urls("https://c.test") == ["https://a.test", "https://b.test"]
    monkeypatch.delenv("RPC_HTTP_POOL")
    assert txmeta.pool_urls("https://c.test") == ["https://c.test"]


class PoolUrlopen:
    def __init__(self, refusing: set[str]) -> None:
        self.refusing = refusing
        self.hits: list[str] = []
        self.agents: list[str] = []

    def __call__(self, request, timeout=None):
        self.hits.append(request.full_url)
        self.agents.append(request.get_header("User-agent") or "")
        if request.full_url in self.refusing:
            raise urllib.error.HTTPError(request.full_url, 429, "too many", {}, None)
        payload = json.loads(request.data)
        items = [{"jsonrpc": "2.0", "id": e["id"], "result": rpc_tx(e["params"][0])} for e in payload]
        return FakeResponse(json.dumps(items).encode())


def test_rpc_client_spreads_chunks_over_the_pool_and_leaves_a_refusing_url_alone(monkeypatch):
    fake = PoolUrlopen(refusing=set())
    monkeypatch.setattr(txmeta, "urlopen", fake)
    monkeypatch.setattr(txmeta, "_limiters", {})
    client = RpcClient("http://a.test", urls=["http://a.test", "http://b.test"], batch_size=1, sleep=no_sleep)

    out = client.batch(
        [
            ("eth_getTransactionByHash", [TX_A]),
            ("eth_getTransactionByHash", [TX_B]),
            ("eth_getTransactionByHash", [TX_C]),
        ]
    )

    assert len(out) == 3 and all("result" in item for item in out)
    assert set(fake.hits) == {"http://a.test", "http://b.test"}
    assert all(agent.startswith("Mozilla/5.0") for agent in fake.agents)

    fake = PoolUrlopen(refusing={"http://a.test"})
    monkeypatch.setattr(txmeta, "urlopen", fake)
    monkeypatch.setattr(txmeta, "_limiters", {})
    client = RpcClient("http://a.test", urls=["http://a.test", "http://b.test"], batch_size=1, sleep=no_sleep)
    out = client.batch([("eth_getTransactionByHash", [TX_A]) for _ in range(6)])
    assert all("result" in item for item in out)
    assert fake.hits.count("http://a.test") <= 2 and fake.hits.count("http://b.test") >= 6
    assert client.limiters["http://a.test"].max_rps < client.limiters["http://b.test"].max_rps


def test_traces_stay_on_the_base_url_while_metadata_uses_the_pool(monkeypatch):
    monkeypatch.setenv("RPC_HTTP_POOL", "https://a.test,https://b.test")
    monkeypatch.setattr(txmeta, "_limiters", {})
    assert TxMetaStore(fake_cur_factory, "https://base.test", store=FakeStore()).rpc.urls == [
        "https://a.test",
        "https://b.test",
    ]
    assert TraceStore(fake_cur_factory, "https://base.test", store=FakeStore()).rpc.urls == ["https://base.test"]


class ForgetfulUrlopen:
    def __init__(self, forgetful: set[str]) -> None:
        self.forgetful = forgetful
        self.hits: list[str] = []

    def __call__(self, request, timeout=None):
        self.hits.append(request.full_url)
        payload = json.loads(request.data)
        if request.full_url in self.forgetful:
            items = [{"jsonrpc": "2.0", "id": e["id"], "result": None} for e in payload]
        else:
            items = [{"jsonrpc": "2.0", "id": e["id"], "result": rpc_tx(e["params"][0])} for e in payload]
        return FakeResponse(json.dumps(items).encode())


def test_a_null_answer_for_history_is_asked_again_of_another_url(monkeypatch):
    fake = ForgetfulUrlopen(forgetful={"http://a.test"})
    monkeypatch.setattr(txmeta, "urlopen", fake)
    monkeypatch.setattr(txmeta, "_limiters", {})
    client = RpcClient("http://a.test", urls=["http://a.test", "http://b.test"], batch_size=1, sleep=no_sleep)

    out = client.batch(
        [("eth_getTransactionByHash", [TX_A]) for _ in range(4)] + [("eth_getTransactionReceipt", [TX_B])]
    )

    assert all(item.get("result") for item in out)
    assert fake.hits.count("http://a.test") <= 2
    assert client.limiters["http://a.test"].max_rps < client.limiters["http://b.test"].max_rps


def test_a_null_answer_from_every_url_is_returned_after_the_attempts_run_out(monkeypatch):
    fake = ForgetfulUrlopen(forgetful={"http://a.test", "http://b.test"})
    monkeypatch.setattr(txmeta, "urlopen", fake)
    monkeypatch.setattr(txmeta, "_limiters", {})
    client = RpcClient(
        "http://a.test", urls=["http://a.test", "http://b.test"], batch_size=1, sleep=no_sleep, max_attempts=3
    )

    out = client.batch([("eth_getTransactionReceipt", [TX_A])])

    assert out[0].get("result") is None and "error" not in out[0]
    assert len(fake.hits) == 3
