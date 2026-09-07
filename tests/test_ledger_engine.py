from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.chain as h  # noqa: E402
from core.ledger.engine import LedgerEngine, Rates  # noqa: E402

DIRECT_URL = os.environ.get("LEDGER_TEST_DATABASE_URL")
RAW_URL = DIRECT_URL or os.environ.get("TEST_DATABASE_URL")
SCRATCH_DB = os.environ.get("SCRATCH_DB_NAME", "crystal_lp_itest") + "_ledger"

TOKEN = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
WALLET = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
SETTLER = "0x0000000000001ff3684f28c67538d4d072c22734"
SETTLER_EXECUTOR = "0x1ab7ea187cee63cf01bbd8fa8837c748a769f8df"
CORE = h.CRYSTAL_ADDR.lower()
MARKET = "0x664fdc46471fd3b407a94e61bc18129abbee3171"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

TF_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PSYNC_TOPIC = "0xc95e30a514d4115dee44b3ba17b2fc114501726562d4c5f2663c06f42df8f1e7"
TR_TOPIC = "0x9adcf0ad0cda63c4d50f26a48925cf6405df27d422a39c456b5f03f661c82982"

SELL_TOKENS = 0x86320487D1CE4000000000
SELL_NATIVE = 0x179C902D8522A53D116
BUY_TOKENS = SELL_TOKENS
BUY_NATIVE = 5_000 * 10**18

BUY_BLOCK = 101_540_000
SELL_BLOCK = 101_543_972
BUY_TX = "0x" + "a1" * 32
SELL_TX = "0xcb3461b56e39f9741e974df5f4a7d0e487d9c042371cd91501a3e6c0130dca5b"

LEDGER_TABLES = ("wallet_flows", "positions_v2", "address_kinds", "venues", "token_registry", "tx_meta", "tx_traces")
SEED_TABLES = ("launchpad_tokens", "crystal_markets", "launchpad_trades", "launchpad_meta")


def _ta(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _w(v: int) -> str:
    return f"{v:064x}"


def _log(blk: int, tx_index: int, log_index: int, txh: str, address: str, topics: list[str], data: str) -> dict:
    return {
        "blockNumber": hex(blk),
        "blockTimestamp": hex(1_757_000_000 + blk),
        "transactionIndex": hex(tx_index),
        "logIndex": hex(log_index),
        "transactionHash": txh,
        "address": address,
        "topics": topics,
        "data": data,
    }


def buy_block_logs() -> list[dict]:
    return [
        _log(BUY_BLOCK, 5, 40, BUY_TX, TOKEN, [TF_TOPIC, _ta(CORE), _ta(WALLET)], "0x" + _w(BUY_TOKENS)),
        _log(BUY_BLOCK, 5, 41, BUY_TX, CORE, [PSYNC_TOPIC, _ta(MARKET)], "0x" + _w(10**22) + _w(10**26)),
        _log(
            BUY_BLOCK,
            5,
            42,
            BUY_TX,
            CORE,
            [TR_TOPIC, _ta(MARKET), _ta(WALLET)],
            "0x" + _w(1) + _w(BUY_NATIVE) + _w(BUY_TOKENS) + _w(3 * 10**13) + _w(4 * 10**13),
        ),
    ]


def sell_block_logs() -> list[dict]:
    return [
        _log(SELL_BLOCK, 3, 26, SELL_TX, TOKEN, [TF_TOPIC, _ta(WALLET), _ta(SETTLER_EXECUTOR)], "0x" + _w(SELL_TOKENS)),
        _log(
            SELL_BLOCK,
            3,
            27,
            SELL_TX,
            CORE,
            [PSYNC_TOPIC, _ta(MARKET)],
            "0x" + _w(0x9849FDFE823D8792B1) + _w(0xBBBBDF35491CF94D77181C),
        ),
        _log(
            SELL_BLOCK,
            3,
            28,
            SELL_TX,
            CORE,
            [TR_TOPIC, _ta(MARKET), _ta(SETTLER_EXECUTOR)],
            "0x" + _w(0) + _w(SELL_TOKENS) + _w(SELL_NATIVE) + _w(4 * 10**13) + _w(3 * 10**13),
        ),
        _log(SELL_BLOCK, 3, 29, SELL_TX, TOKEN, [TF_TOPIC, _ta(SETTLER_EXECUTOR), _ta(CORE)], "0x" + _w(SELL_TOKENS)),
    ]


class FakeTxMeta:
    def __init__(self, metas: dict):
        self.metas = metas
        self.requested: list[list[str]] = []

    def get_many(self, txhashes):
        self.requested.append(list(txhashes))
        return {txh: self.metas[txh] for txh in txhashes if txh in self.metas}


class FakeTrace:
    def __init__(self):
        self.requested: list[str] = []

    def native_transfers(self, txhash):
        from core.ledger.types import TraceResult

        self.requested.append(txhash)
        return TraceResult(available=False, transfers=[])


class FakeKinds:
    def __init__(self, kinds: dict[str, str]):
        self.kinds = kinds
        self.loaded = 0
        self.tx_venues: frozenset[str] = frozenset()
        self.discover: dict[str, list[str]] = {}

    def load_known(self, cur):
        self.loaded += 1

    def kind(self, addr, cur):
        return self.kinds.get(addr.lower(), "eoa")

    def is_wallet(self, kind):
        return kind in {"eoa", "eoa_7702", "wallet_4337", "contract_unknown"}

    def observe_tx(self, bundle, registry, cur=None):
        return list(self.discover.get(bundle.txhash, []))

    def userop_sender(self, bundle, cur=None):
        return None


def fixture_kinds() -> FakeKinds:
    return FakeKinds(
        {
            WALLET: "eoa",
            SETTLER: "venue_router",
            SETTLER_EXECUTOR: "venue_router",
            CORE: "venue_curve",
            MARKET: "venue_pool",
            TOKEN: "token",
            WMON: "token",
            "0x" + "0" * 40: "zero",
        }
    )


def fixture_metas() -> dict:
    from core.ledger.types import TxMeta

    return {
        BUY_TX: TxMeta(
            txhash=BUY_TX,
            block_number=BUY_BLOCK,
            tx_index=5,
            from_addr=WALLET,
            to_addr=CORE,
            value=BUY_NATIVE,
            selector="0x12345678",
        ),
        SELL_TX: TxMeta(
            txhash=SELL_TX,
            block_number=SELL_BLOCK,
            tx_index=3,
            from_addr=WALLET,
            to_addr=SETTLER,
            value=0,
            selector="0x1fff991f",
        ),
    }


def fixed_rates(blk, ts, cur) -> Rates:
    return Rates(mon_usd=Decimal("0.03"), lvmon_rate=Decimal(1), usdc_per_mon=Decimal("0.03"))


def test_build_bundles_groups_logs_by_transaction_and_names_the_market_token():
    engine = LedgerEngine(cur_factory=None, enabled=True)
    engine._registry = {TOKEN: object()}
    engine._market_tokens = {MARKET: TOKEN}
    logs = sell_block_logs() + buy_block_logs()

    bundles, moved = engine.build_bundles(SELL_BLOCK, 1_700_000_000, logs, cur=None)

    assert [b.txhash for b in bundles] == [SELL_TX, BUY_TX]
    assert moved == {SELL_TX, BUY_TX}
    sell = bundles[0]
    assert sell.tx_index == 3
    assert [t.log_index for t in sell.transfers] == [26, 29]
    assert sell.transfers[0].from_addr == WALLET and sell.transfers[0].to_addr == SETTLER_EXECUTOR
    assert sell.transfers[0].amount == SELL_TOKENS
    assert [e.tag for e in sell.venue_events] == ["PSYNC", "LT"]
    trade = sell.venue_events[1].parsed
    assert trade["token"] == TOKEN
    assert trade["market"] == MARKET
    assert trade["user"] == SETTLER_EXECUTOR
    assert trade["is_buy"] is False
    assert trade["amount_in"] == SELL_TOKENS and trade["amount_out"] == SELL_NATIVE
    assert sell.meta is None and sell.trace is None and sell.userop_sender is None


def test_build_bundles_ignores_unregistered_tokens_for_meta_fetching():
    engine = LedgerEngine(cur_factory=None, enabled=True)
    engine._registry = {}
    engine._market_tokens = {}

    bundles, moved = engine.build_bundles(BUY_BLOCK, 1, buy_block_logs(), cur=None)

    assert len(bundles) == 1 and moved == set()


def test_enabled_flag_reads_env(monkeypatch):
    monkeypatch.delenv("LEDGER_ENABLED", raising=False)
    assert LedgerEngine(cur_factory=None).enabled is False
    monkeypatch.setenv("LEDGER_ENABLED", "true")
    assert LedgerEngine(cur_factory=None).enabled is True
    monkeypatch.setenv("LEDGER_ENABLED", "0")
    assert LedgerEngine(cur_factory=None).enabled is False


pytestmark_db = pytest.mark.skipif(not RAW_URL, reason="set LEDGER_TEST_DATABASE_URL or TEST_DATABASE_URL")


def _swap_db(url: str, dbname: str) -> str:
    head, _, tail = url.rpartition("/")
    query = ""
    if "?" in tail:
        _, _, query = tail.partition("?")
        query = "?" + query
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def db():
    import psycopg2

    from core.ledger.schema import init_ledger_schema
    from core.storage import base as storage_base

    if DIRECT_URL:
        url = DIRECT_URL
    else:
        url = _swap_db(RAW_URL, SCRATCH_DB)
        conn = psycopg2.connect(RAW_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{SCRATCH_DB}';")
            cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB};")
            cur.execute(f"CREATE DATABASE {SCRATCH_DB};")
        conn.close()

    storage_base._DATABASE_URL = url
    storage_base._POOL = None

    import core.storage as storage

    storage.init_pool()
    storage.init_db()
    with storage.db_cursor() as cur:
        init_ledger_schema(cur)

    yield url

    try:
        storage_base.close_pool()
    except Exception:
        pass
    storage_base._POOL = None
    if not DIRECT_URL:
        conn = psycopg2.connect(RAW_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{SCRATCH_DB}';")
            cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB};")
        conn.close()


@pytest.fixture
def seeded(db):
    import psycopg2

    conn = psycopg2.connect(db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(LEDGER_TABLES + SEED_TABLES) + " RESTART IDENTITY CASCADE;")
        cur.execute(
            """
            INSERT INTO launchpad_tokens (token, creator, name, symbol, source, created_block, created_at, market)
            VALUES (%s, %s, 'Chipotle', 'CHIPOTLE', 0, %s, %s, %s)
            """,
            (TOKEN, WALLET, BUY_BLOCK - 1000, 1_756_999_000, MARKET),
        )
        cur.execute(
            """
            INSERT INTO crystal_markets (market, is_canonical, quote_asset, base_asset, quote_address, quote_decimals,
                quote_ticker, quote_name, base_address, base_decimals, base_ticker, base_name, created_block)
            VALUES (%s, true, 'MON', 'CHIPOTLE', %s, 18, 'MON', 'Monad', %s, 18, 'CHIPOTLE', 'Chipotle', %s)
            """,
            (MARKET, WMON, TOKEN, BUY_BLOCK - 1000),
        )
        cur.execute("INSERT INTO launchpad_meta (key, value) VALUES ('mon_price_usd', 0.03), ('lvmon_mon_rate', 1)")
    conn.close()
    yield db


OTHER_TOKEN = "0x" + "b2" * 20
WALLET2 = "0x" + "c3" * 20
WALLET3 = "0x" + "d4" * 20
OTHER_TX = "0x" + "e5" * 32


def other_token_transfer_logs() -> list[dict]:
    return [
        _log(BUY_BLOCK, 9, 70, OTHER_TX, OTHER_TOKEN, [TF_TOPIC, _ta(WALLET2), _ta(WALLET3)], "0x" + _w(7 * 10**18))
    ]


def fixture_engine(db_cursor, metas=None, kinds=None):
    return LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=metas or FakeTxMeta(fixture_metas()),
        trace_store=FakeTrace(),
        kinds=kinds or fixture_kinds(),
        rates_fn=fixed_rates,
        head_fn=lambda: SELL_BLOCK + 1_000_000,
    )


@pytestmark_db
def test_a_token_touched_only_incidentally_gets_flows_but_no_position(seeded):
    """Replaying TOKEN from creation also sees OTHER_TOKEN move in the same block.

    Its flows are evidence and are kept; its position would be a fragment of an unreplayed history, and that
    fragment is where every negative balance in the leftover tokens came from (feedback2.md finding 7).
    """
    import psycopg2

    from core.storage import db_cursor

    conn = psycopg2.connect(seeded)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO launchpad_tokens (token, creator, name, symbol, source, created_block, created_at) "
            "VALUES (%s, %s, 'Other', 'OTHER', 0, %s, %s)",
            (OTHER_TOKEN, WALLET2, BUY_BLOCK - 5000, 1_756_990_000),
        )
    conn.close()
    kinds = fixture_kinds()
    kinds.kinds[WALLET2] = "eoa"
    kinds.kinds[WALLET3] = "eoa"
    kinds.kinds[OTHER_TOKEN] = "token"
    engine = fixture_engine(db_cursor, kinds=kinds)
    engine.scope = frozenset({TOKEN})
    with db_cursor() as cur:
        engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs() + other_token_transfer_logs(), cur)
        engine.cover(cur, [TOKEN], BUY_BLOCK - 1000, BUY_BLOCK)
        engine.flush(cur)
        cur.execute("SELECT count(*) FROM wallet_flows WHERE token = %s", (OTHER_TOKEN,))
        other_flows = cur.fetchone()[0]
        cur.execute("SELECT token, count(*) FROM positions_v2 GROUP BY token ORDER BY token")
        positions = dict(cur.fetchall())
    assert other_flows == 2, "the movement is evidence and must be kept"
    assert OTHER_TOKEN not in positions, "a token with no coverage from creation must not be served as positions"
    assert positions.get(TOKEN) == 1


@pytestmark_db
def test_replaying_a_block_again_replaces_what_an_earlier_run_wrote_for_the_scoped_token(seeded):
    """A replay is the authority for its scope in the blocks it covers; first-writer-wins is not (feedback7 P1-2)."""
    from dataclasses import replace as dc_replace

    from core.ledger import store
    from core.storage import db_cursor

    engine = fixture_engine(db_cursor)
    engine.scope = frozenset({TOKEN})
    with db_cursor() as cur:
        engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur)
        buy = store.load_flows(cur, [(WALLET, TOKEN)])[(WALLET, TOKEN)][0]
        cur.execute("DELETE FROM wallet_flows")
        stale = dc_replace(buy, kind="sell", token_delta=-buy.token_delta, wallet=WALLET2)
        assert store.insert_flows(cur, [stale]) == 1
    engine = fixture_engine(db_cursor)
    engine.scope = frozenset({TOKEN})
    with db_cursor() as cur:
        engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur)
        cur.execute("SELECT wallet, kind FROM wallet_flows WHERE block_number = %s", (BUY_BLOCK,))
        rows = cur.fetchall()
    assert rows == [(WALLET, "buy")], "the earlier run's row at the same key must be replaced, not kept"


@pytestmark_db
def test_engine_folds_curve_buy_and_settler_sell_into_one_closed_position(seeded):
    from core.ledger import store
    from core.storage import db_cursor

    metas = FakeTxMeta(fixture_metas())
    traces = FakeTrace()
    kinds = fixture_kinds()
    engine = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=metas,
        trace_store=traces,
        kinds=kinds,
        rates_fn=fixed_rates,
        head_fn=lambda: SELL_BLOCK + 1_000_000,
    )

    with db_cursor() as cur:
        inserted_buy = engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur)
        inserted_sell = engine.process_block(SELL_BLOCK, 1_757_000_000 + SELL_BLOCK, sell_block_logs(), cur)
        assert engine.affected_keys() == [TOKEN]
        refolded = engine.flush(cur)

    assert inserted_buy == 1
    assert inserted_sell == 1
    assert refolded == 1
    assert engine.affected_keys() == []
    assert metas.requested == [[BUY_TX], [SELL_TX]]
    assert traces.requested == []
    assert kinds.loaded == 1

    with db_cursor() as cur:
        assert TOKEN in engine.registry(cur)
        flows = store.load_flows(cur, [(WALLET, TOKEN)])[(WALLET, TOKEN)]
        cur.execute("SELECT DISTINCT wallet FROM wallet_flows")
        wallets = {r[0] for r in cur.fetchall()}
        cur.execute(
            """
            SELECT balance_token, custody_balance, token_bought, token_sold, native_spent, native_received,
                   cost_basis_native, realized_pnl_native, basis_estimated_native, realized_estimated_native,
                   unresolved_tokens, unresolved_proceeds_native, trade_count, buy_count, sell_count,
                   first_flow_ts, last_flow_ts, last_flow_block, flow_count
            FROM positions_v2 WHERE wallet = %s AND token = %s
            """,
            (WALLET, TOKEN),
        )
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM positions_v2")
        position_rows = cur.fetchone()[0]

    assert wallets == {WALLET}
    assert [f.kind for f in flows] == ["buy", "sell"]
    buy, sell = flows
    assert (buy.block_number, buy.tx_index, buy.log_index) == (BUY_BLOCK, 5, 40)
    assert buy.txhash == BUY_TX
    assert int(buy.token_delta) == BUY_TOKENS
    assert int(buy.quote_delta) == -BUY_NATIVE
    assert buy.basis_state == "observed"
    assert buy.venue == CORE
    assert buy.origin == WALLET
    assert (sell.block_number, sell.tx_index) == (SELL_BLOCK, 3)
    assert sell.txhash == SELL_TX
    assert int(sell.token_delta) == -SELL_TOKENS
    assert int(sell.quote_delta) == SELL_NATIVE
    assert sell.basis_state == "observed"
    assert sell.source == "venue_event"
    assert sell.origin == WALLET
    assert Decimal(sell.mon_value) == Decimal(SELL_NATIVE) / Decimal(10**18)
    expected_usd = Decimal(SELL_NATIVE) * Decimal("0.03") / Decimal(10**18)
    assert abs(Decimal(sell.usd_value) - expected_usd) < Decimal("1e-12")
    assert int(sell.realized_delta) == SELL_NATIVE - BUY_NATIVE

    assert row is not None
    assert position_rows == 1
    (
        balance,
        custody,
        bought,
        sold,
        spent,
        received,
        basis,
        realized,
        basis_est,
        realized_est,
        unresolved,
        unresolved_proceeds,
        trade_count,
        buy_count,
        sell_count,
        first_ts,
        last_ts,
        last_block,
        flow_count,
    ) = row
    assert int(balance) == 0
    assert int(custody) == 0
    assert int(bought) == BUY_TOKENS
    assert int(sold) == SELL_TOKENS
    assert int(spent) == BUY_NATIVE
    assert int(received) == SELL_NATIVE
    assert int(basis) == 0
    assert int(realized) == SELL_NATIVE - BUY_NATIVE
    assert int(basis_est) == 0 and int(realized_est) == 0
    assert int(unresolved) == 0 and int(unresolved_proceeds) == 0
    assert (trade_count, buy_count, sell_count) == (2, 1, 1)
    assert first_ts == 1_757_000_000 + BUY_BLOCK
    assert last_ts == 1_757_000_000 + SELL_BLOCK
    assert last_block == SELL_BLOCK
    assert flow_count == 2


@pytestmark_db
def test_reprocessing_a_block_is_idempotent(seeded):
    from core.storage import db_cursor

    engine = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=FakeTxMeta(fixture_metas()),
        trace_store=FakeTrace(),
        kinds=fixture_kinds(),
        rates_fn=fixed_rates,
        head_fn=lambda: SELL_BLOCK + 1_000_000,
    )
    with db_cursor() as cur:
        first = engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur)
        engine.flush(cur)
        again = engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur)
        engine.flush(cur)
        cur.execute("SELECT count(*) FROM wallet_flows")
        flow_rows = cur.fetchone()[0]
        cur.execute(
            "SELECT token_bought, trade_count FROM positions_v2 WHERE wallet = %s AND token = %s", (WALLET, TOKEN)
        )
        bought, trade_count = cur.fetchone()

    assert first == 1
    assert again == 0
    assert flow_rows == 1
    assert int(bought) == BUY_TOKENS
    assert trade_count == 1


TC_TOPIC = "0x24ad3570873d98f204dae563a92a783a01f6935a8965547ce8bf2cadd2c6ce3b"
NEW_TOKEN = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
NEW_TX = "0x" + "b2" * 32


def _string_tail(value: str) -> str:
    raw = value.encode()
    padded = ((len(raw) + 31) // 32) * 32
    return _w(len(raw)) + raw.hex().ljust(padded * 2, "0")


def _tc_data(strings: list[str]) -> str:
    tails = [_string_tail(s) for s in strings]
    offset = len(strings) * 32
    heads = []
    for tail in tails:
        heads.append(_w(offset))
        offset += len(tail) // 2
    return "0x" + "".join(heads) + "".join(tails)


@pytestmark_db
def test_token_created_in_the_same_block_is_registered_before_its_first_trade(seeded):
    from core.ledger.types import TxMeta
    from core.storage import db_cursor

    blk = BUY_BLOCK + 1
    logs = [
        _log(
            blk,
            1,
            10,
            NEW_TX,
            CORE,
            [TC_TOPIC, _ta(NEW_TOKEN), _ta(WALLET)],
            _tc_data(["Fresh", "FRESH", "cid", "desc", "", ""]),
        ),
        _log(blk, 1, 11, NEW_TX, NEW_TOKEN, [TF_TOPIC, _ta(CORE), _ta(WALLET)], "0x" + _w(10**24)),
    ]
    meta = TxMeta(
        txhash=NEW_TX, block_number=blk, tx_index=1, from_addr=WALLET, to_addr=CORE, value=10**18, selector="0x"
    )
    engine = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=FakeTxMeta({NEW_TX: meta}),
        trace_store=FakeTrace(),
        kinds=fixture_kinds(),
        rates_fn=fixed_rates,
        head_fn=lambda: blk + 1_000_000,
    )
    with db_cursor() as cur:
        inserted = engine.process_block(blk, 1_757_000_000 + blk, logs, cur)
        engine.flush(cur)
        registry = engine.registry(cur)
        cur.execute("SELECT source, registered_block, quote_token FROM token_registry WHERE token = %s", (NEW_TOKEN,))
        reg_row = cur.fetchone()
        cur.execute(
            "SELECT kind, token_delta, quote_delta FROM wallet_flows WHERE wallet = %s AND token = %s",
            (WALLET, NEW_TOKEN),
        )
        flow_row = cur.fetchone()

    assert inserted == 1
    assert NEW_TOKEN in registry
    assert reg_row == ("crystal", blk, WMON)
    assert flow_row[0] == "buy"
    assert int(flow_row[1]) == 10**24
    assert int(flow_row[2]) == -(10**18)


@pytestmark_db
def test_trace_is_requested_only_inside_the_archive_window(seeded):
    from core.storage import db_cursor

    class UnresolvedMeta(FakeTxMeta):
        def get_many(self, txhashes):
            from core.ledger.types import TxMeta

            self.requested.append(list(txhashes))
            return {
                txh: TxMeta(
                    txhash=txh,
                    block_number=BUY_BLOCK,
                    tx_index=5,
                    from_addr=WALLET,
                    to_addr=CORE,
                    value=0,
                    selector="0x",
                )
                for txh in txhashes
            }

    logs = [buy_block_logs()[0]]
    traces_near = FakeTrace()
    near = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=UnresolvedMeta({}),
        trace_store=traces_near,
        kinds=fixture_kinds(),
        rates_fn=fixed_rates,
        head_fn=lambda: BUY_BLOCK + 10,
    )
    traces_far = FakeTrace()
    far = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=UnresolvedMeta({}),
        trace_store=traces_far,
        kinds=fixture_kinds(),
        rates_fn=fixed_rates,
        head_fn=lambda: BUY_BLOCK + 1_000_000,
    )
    with db_cursor() as cur:
        near.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, logs, cur)
        near.flush(cur)
        cur.execute("TRUNCATE wallet_flows, positions_v2")
        far.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, logs, cur)
        far.flush(cur)

    assert traces_near.requested == [BUY_TX]
    assert traces_far.requested == []


@pytestmark_db
def test_discovering_a_venue_purges_the_rows_it_earned_as_a_wallet(seeded):
    from core.storage import db_cursor

    kinds = fixture_kinds()
    kinds.discover = {SELL_TX: [WALLET]}
    engine = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=FakeTxMeta(fixture_metas()),
        trace_store=FakeTrace(),
        kinds=kinds,
        rates_fn=fixed_rates,
        head_fn=lambda: SELL_BLOCK + 1_000_000,
    )

    with db_cursor() as cur:
        assert engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur) == 1
        engine.flush(cur)
        cur.execute("SELECT count(*) FROM positions_v2")
        assert cur.fetchone()[0] == 1
        kinds.kinds[WALLET] = "venue_pool"
        assert engine.process_block(SELL_BLOCK, 1_757_000_000 + SELL_BLOCK, sell_block_logs(), cur) == 0
        assert engine.affected_keys() == [TOKEN], "the purge invalidated this token's fold, so it must be refolded"
        engine.flush(cur)
        cur.execute("SELECT count(*) FROM wallet_flows")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM positions_v2")
        assert cur.fetchone()[0] == 0
    assert engine.stats["purged"] == 1


@pytestmark_db
def test_pool_shaped_contracts_of_the_transaction_are_netted_as_venues(seeded):
    from core.storage import db_cursor

    kinds = fixture_kinds()
    kinds.kinds[SETTLER_EXECUTOR] = "contract_unknown"
    kinds.tx_venues = frozenset({SETTLER_EXECUTOR})
    engine = LedgerEngine(
        cur_factory=db_cursor,
        enabled=True,
        tx_meta_store=FakeTxMeta(fixture_metas()),
        trace_store=FakeTrace(),
        kinds=kinds,
        rates_fn=fixed_rates,
        head_fn=lambda: SELL_BLOCK + 1_000_000,
    )
    with db_cursor() as cur:
        assert engine.process_block(SELL_BLOCK, 1_757_000_000 + SELL_BLOCK, sell_block_logs(), cur) == 1
        cur.execute("SELECT DISTINCT wallet FROM wallet_flows")
        assert {r[0] for r in cur.fetchall()} == {WALLET}


def test_reference_price_is_the_median_of_recent_sized_observed_trades_only():
    from decimal import Decimal

    from core.ledger.engine import LedgerEngine

    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def fetchall(self):
            return [
                (Decimal("0.021"),),
                (Decimal("0.019"),),
                (Decimal("0.020"),),
                (Decimal("5"),),
                (Decimal("0.0201"),),
            ]

        def fetchone(self):
            return None

    engine = LedgerEngine(cur_factory=None, enabled=True)
    cur = Cur()
    assert engine._reference_price("0xtoken", 100, 1_700_000_000, cur) == Decimal("0.0201")
    assert "basis_state = 'observed'" in cur.sql[0]
    assert "abs(token_delta) >= %s" in cur.sql[0]


def transfer_block_logs(blk, sender, receiver, amount, txh):
    return [_log(blk, 2, 10, txh, TOKEN, [TF_TOPIC, _ta(sender), _ta(receiver)], "0x" + _w(amount))]


@pytestmark_db
def test_a_transfer_carries_its_cost_all_the_way_into_the_stored_positions(seeded):
    """The whole path: buy, hand the tokens on, sell them from the other wallet.

    Folded wallet by wallet the receiver's tokens arrive priceless and the sale reads as pure profit, which
    is the phantom-profit shape the rebuild exists to remove. Both halves must survive storage for this to
    work at all, so this also pins the primary key that used to collapse them into one row.
    """
    from core.ledger.types import TxMeta
    from core.storage import db_cursor

    transfer_block, sale_block = BUY_BLOCK + 10, BUY_BLOCK + 20
    transfer_tx, sale_tx = "0x" + "77" * 32, "0x" + "88" * 32
    metas = dict(fixture_metas())
    metas[transfer_tx] = TxMeta(transfer_tx, transfer_block, 2, WALLET, WALLET2, 0, "0xa9059cbb")
    metas[sale_tx] = TxMeta(sale_tx, sale_block, 3, WALLET2, CORE, 0, "0x1fff991f")
    kinds = fixture_kinds()
    kinds.kinds[WALLET2] = "eoa"
    engine = fixture_engine(db_cursor, metas=FakeTxMeta(metas), kinds=kinds)
    engine.scope = frozenset({TOKEN})

    sale = [
        _log(sale_block, 3, 26, sale_tx, TOKEN, [TF_TOPIC, _ta(WALLET2), _ta(CORE)], "0x" + _w(BUY_TOKENS)),
        _log(
            sale_block,
            3,
            28,
            sale_tx,
            CORE,
            [TR_TOPIC, _ta(MARKET), _ta(WALLET2)],
            "0x" + _w(0) + _w(BUY_TOKENS) + _w(SELL_NATIVE) + _w(0) + _w(0),
        ),
    ]
    with db_cursor() as cur:
        engine.process_block(BUY_BLOCK, 1_757_000_000 + BUY_BLOCK, buy_block_logs(), cur)
        engine.process_block(
            transfer_block,
            1_757_000_000 + transfer_block,
            transfer_block_logs(transfer_block, WALLET, WALLET2, BUY_TOKENS, transfer_tx),
            cur,
        )
        engine.process_block(sale_block, 1_757_000_000 + sale_block, sale, cur)
        engine.cover(cur, [TOKEN], BUY_BLOCK - 1000, sale_block)
        engine.flush(cur)

        cur.execute(
            "SELECT wallet, kind, sub_index, basis_delta FROM wallet_flows WHERE block_number = %s ORDER BY sub_index",
            (transfer_block,),
        )
        halves = cur.fetchall()
        cur.execute(
            "SELECT wallet, balance_token, cost_basis_native, realized_pnl_native, unresolved_tokens "
            "FROM positions_v2 WHERE token = %s ORDER BY wallet",
            (TOKEN,),
        )
        positions = {row[0]: row[1:] for row in cur.fetchall()}

    assert [(w, k, s) for w, k, s, _ in halves] == [(WALLET, "transfer_out", 0), (WALLET2, "transfer_in", 1)]
    assert halves[0][3] == -BUY_NATIVE and halves[1][3] == BUY_NATIVE, (
        "the cost leaves one wallet and reaches the other"
    )
    assert positions[WALLET] == (0, 0, 0, 0), "the sender keeps neither the tokens nor their cost, and books no gain"
    balance, basis, realized, unresolved = positions[WALLET2]
    assert (balance, basis, unresolved) == (0, 0, 0)
    assert realized == SELL_NATIVE - BUY_NATIVE, "the sale is priced against what the sender originally paid"
