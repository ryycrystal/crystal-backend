from __future__ import annotations

import os
import sys
from dataclasses import replace
from decimal import Decimal
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DIRECT_URL = os.environ.get("LEDGER_TEST_DATABASE_URL")
RAW_URL = DIRECT_URL or os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
SCRATCH_DB = os.environ.get("SCRATCH_DB_NAME", "crystal_lp_itest") + "_ledger_store"
PROD_TUNNEL_PORT = 15433

pytestmark = pytest.mark.skipif(
    not RAW_URL, reason="set LEDGER_TEST_DATABASE_URL (or DATABASE_URL) to the side database to run ledger store tests"
)

WALLET_A = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
WALLET_B = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
TOKEN_X = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
TOKEN_Y = "0x405b6330e213ded490240cbcdd64790806827777"
VENUE = "0x6eb2af5fc575689053ac9b413220cabfd01a2f9a"
TX_1 = "0xcb3461b56e39f9741e974df5f4a7d0e487d9c042371cd91501a3e6c0130dca5b"
TX_2 = "0x222182027db3846107aa51edc65c4f025fac4c8428e249dabe03a75b54a57b28"
BIG = 3173915918780000000000000000


def _local_only(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1") or parsed.port == PROD_TUNNEL_PORT:
        pytest.skip("ledger store tests only run against a local side database")


def _swap_db(url: str, dbname: str) -> str:
    head, _, tail = url.rpartition("/")
    query = ""
    if "?" in tail:
        _, _, query = tail.partition("?")
        query = "?" + query
    return f"{head}/{dbname}{query}"


def _scratch(action: str) -> None:
    import psycopg2

    connection = psycopg2.connect(RAW_URL)
    connection.autocommit = True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (SCRATCH_DB,))
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
        if action == "create":
            cur.execute(f"CREATE DATABASE {SCRATCH_DB}")
    connection.close()


def _ensure_base_schema(conn, url: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.launchpad_blocks')")
        present = cur.fetchone()[0] is not None
    if present:
        return
    from core.storage import base as storage_base

    previous_url = storage_base._DATABASE_URL
    storage_base._DATABASE_URL = url
    storage_base._POOL = None
    try:
        import core.storage as storage

        storage.init_pool()
        storage.init_db()
    finally:
        try:
            storage_base.close_pool()
        except Exception:
            pass
        storage_base._POOL = None
        storage_base._DATABASE_URL = previous_url


@pytest.fixture(scope="module")
def conn():
    import psycopg2

    _local_only(RAW_URL)
    if DIRECT_URL:
        url = DIRECT_URL
    else:
        _scratch("create")
        url = _swap_db(RAW_URL, SCRATCH_DB)
    connection = psycopg2.connect(url)
    connection.autocommit = True
    _ensure_base_schema(connection, url)
    from core.ledger.schema import init_ledger_schema

    with connection.cursor() as cur:
        init_ledger_schema(cur)
    connection.autocommit = False
    yield connection
    connection.rollback()
    connection.close()
    if not DIRECT_URL:
        _scratch("drop")


@pytest.fixture
def cur(conn):
    from core.ledger import store
    from core.ledger.schema import LEDGER_TABLES

    store.invalidate_registry()
    conn.rollback()
    with conn.cursor() as setup:
        setup.execute("TRUNCATE " + ", ".join(LEDGER_TABLES))
    conn.commit()
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()
        conn.rollback()
        store.invalidate_registry()


def _flow(**overrides):
    from core.ledger.types import Flow

    values = {
        "block_number": 100,
        "tx_index": 1,
        "log_index": 2,
        "sub_index": 0,
        "txhash": TX_1,
        "timestamp": 1_700_000_000,
        "wallet": WALLET_A,
        "token": TOKEN_X,
        "token_delta": 10**18,
        "quote_asset": "native",
        "quote_delta": -(5 * 10**17),
        "mon_value": Decimal("0.5"),
        "usd_value": Decimal("1.25"),
        "kind": "buy",
        "venue": VENUE,
        "counterparty": VENUE,
        "origin": WALLET_A,
        "source": "venue_event",
        "basis_state": "observed",
        "price_native": Decimal("0.5"),
        "basis_delta": 0,
        "realized_delta": 0,
    }
    values.update(overrides)
    return Flow(**values)


def _position(**overrides):
    from core.ledger.types import PositionRow

    values = {
        "wallet": WALLET_A,
        "token": TOKEN_X,
        "balance_token": 0,
        "custody_balance": 0,
        "token_bought": 0,
        "token_sold": 0,
        "native_spent": 0,
        "native_received": 0,
        "cost_basis_native": 0,
        "realized_pnl_native": 0,
        "basis_estimated_native": 0,
        "realized_estimated_native": 0,
        "unresolved_tokens": 0,
        "unresolved_proceeds_native": 0,
        "trade_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "first_flow_ts": None,
        "last_flow_ts": None,
        "last_flow_block": None,
        "flow_count": 0,
    }
    values.update(overrides)
    return PositionRow(**values)


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def test_ledger_schema_is_idempotent_and_never_touches_existing_tables(conn, cur):
    from core.ledger.schema import _STATEMENTS, LEDGER_TABLES, init_ledger_schema

    cur.execute("CREATE TABLE IF NOT EXISTS ledger_test_bystander (id INT PRIMARY KEY, note TEXT)")
    cur.execute("INSERT INTO ledger_test_bystander VALUES (1, 'keep') ON CONFLICT DO NOTHING")
    init_ledger_schema(cur)
    init_ledger_schema(cur)
    for table in LEDGER_TABLES:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        assert cur.fetchone()[0] == table
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'wallet_flows' ORDER BY 1")
    names = [row[0] for row in cur.fetchall()]
    assert "idx_wallet_flows_wallet_token_pos" in names
    assert "idx_wallet_flows_token_block" in names
    cur.execute("SELECT note FROM ledger_test_bystander WHERE id = 1")
    assert cur.fetchone()[0] == "keep"
    cur.execute("DROP TABLE ledger_test_bystander")
    for statement in _STATEMENTS:
        head = " ".join(statement.split())
        assert head.startswith("CREATE TABLE IF NOT EXISTS") or head.startswith("CREATE INDEX IF NOT EXISTS")


def test_insert_flows_ignores_primary_key_conflicts(cur):
    from core.ledger import store

    first = [_flow(log_index=1), _flow(log_index=2), _flow(log_index=2, sub_index=1, wallet=WALLET_B)]
    assert store.insert_flows(cur, first) == 3
    again = [replace(f, kind="sell") for f in first] + [_flow(log_index=3, block_number=101)]
    assert store.insert_flows(cur, again) == 1
    assert _count(cur, "wallet_flows") == 4
    cur.execute("SELECT kind FROM wallet_flows WHERE block_number = 100 AND log_index = 1")
    assert cur.fetchone()[0] == "buy"
    assert store.insert_flows(cur, [_flow(log_index=9), _flow(log_index=9)]) == 1
    assert store.insert_flows(cur, []) == 0


def test_insert_flows_round_trips_every_column(cur):
    from core.ledger import store

    flow = _flow(
        token_delta=-BIG,
        quote_delta=BIG * 3,
        mon_value=Decimal("34574.254123456789012345"),
        usd_value=Decimal("0.000000000000000001"),
        price_native=None,
        quote_asset=None,
        venue=None,
        counterparty=WALLET_B.upper(),
        origin=None,
        kind="transfer_out",
        source="transfer_net",
        basis_state="unresolved",
        basis_delta=-(10**30),
        realized_delta=7,
    )
    assert store.insert_flows(cur, [flow]) == 1
    loaded = store.load_flows(cur, [(WALLET_A, TOKEN_X)])[(WALLET_A, TOKEN_X)]
    assert loaded == [replace(flow, counterparty=WALLET_B)]
    assert isinstance(loaded[0].token_delta, int)
    assert isinstance(loaded[0].mon_value, Decimal)


def test_load_flows_orders_by_chain_position_per_key(cur):
    from core.ledger import store

    flows = [
        _flow(block_number=200, tx_index=0, log_index=5, sub_index=0),
        _flow(block_number=100, tx_index=3, log_index=1, sub_index=1),
        _flow(block_number=100, tx_index=3, log_index=1, sub_index=0),
        _flow(block_number=100, tx_index=2, log_index=9, sub_index=0),
        _flow(block_number=150, tx_index=0, log_index=0, sub_index=0, wallet=WALLET_B, token=TOKEN_Y),
        _flow(block_number=120, tx_index=0, log_index=0, sub_index=0, wallet=WALLET_B, token=TOKEN_Y),
        _flow(block_number=130, tx_index=0, log_index=0, sub_index=0, wallet=WALLET_B, token=TOKEN_X),
    ]
    assert store.insert_flows(cur, flows) == 7
    out = store.load_flows(cur, [(WALLET_A.upper(), TOKEN_X), (WALLET_B, TOKEN_Y), (WALLET_A, TOKEN_Y)])
    assert set(out) == {(WALLET_A, TOKEN_X), (WALLET_B, TOKEN_Y), (WALLET_A, TOKEN_Y)}
    positions = [(f.block_number, f.tx_index, f.log_index, f.sub_index) for f in out[(WALLET_A, TOKEN_X)]]
    assert positions == [(100, 2, 9, 0), (100, 3, 1, 0), (100, 3, 1, 1), (200, 0, 5, 0)]
    assert [f.block_number for f in out[(WALLET_B, TOKEN_Y)]] == [120, 150]
    assert out[(WALLET_A, TOKEN_Y)] == []
    assert store.load_flows(cur, []) == {}


def test_load_flows_handles_more_keys_than_one_chunk(cur):
    from core.ledger import store

    wallets = [f"0x{i:040x}" for i in range(1, 1203)]
    flows = [_flow(block_number=i, wallet=wallet) for i, wallet in enumerate(wallets, start=1)]
    assert store.insert_flows(cur, flows) == len(wallets)
    out = store.load_flows(cur, [(wallet, TOKEN_X) for wallet in wallets])
    assert len(out) == len(wallets)
    assert all(len(v) == 1 for v in out.values())


def test_upsert_positions_replaces_the_whole_row(cur):
    from core.ledger import store

    store.upsert_positions(cur, [_position(balance_token=5, trade_count=1, first_flow_ts=10, last_flow_ts=10)])
    store.upsert_positions(
        cur,
        [
            _position(wallet=WALLET_A.upper(), balance_token=BIG, cost_basis_native=42, trade_count=3),
            _position(wallet=WALLET_B, balance_token=1),
            _position(wallet=WALLET_B, balance_token=2, token_sold=9),
        ],
    )
    cur.execute(
        "SELECT wallet, balance_token, cost_basis_native, trade_count, first_flow_ts, token_sold FROM positions_v2 ORDER BY 1"
    )
    rows = cur.fetchall()
    assert rows == [
        (WALLET_A, Decimal(BIG), Decimal(42), 3, None, Decimal(0)),
        (WALLET_B, Decimal(2), Decimal(0), 0, None, Decimal(9)),
    ]
    store.upsert_positions(cur, [])
    assert _count(cur, "positions_v2") == 2


def _sum_fold(prev, flows):
    balance = sum(f.token_delta for f in flows)
    row = _position(
        wallet="",
        token="",
        balance_token=balance,
        flow_count=len(flows),
        first_flow_ts=flows[0].timestamp,
        last_flow_ts=flows[-1].timestamp,
        last_flow_block=flows[-1].block_number,
    )
    return row, [replace(f, basis_delta=f.token_delta, realized_delta=-f.token_delta) for f in flows]


class _State:
    def __init__(self, balance: int) -> None:
        self.balance = balance

    def to_row(self):
        return _position(balance_token=self.balance, flow_count=1)


def test_refold_round_trip_writes_positions_and_fold_deltas(cur):
    from core.ledger import store

    flows = [
        _flow(block_number=100, log_index=1, token_delta=10),
        _flow(block_number=101, log_index=1, token_delta=-4),
        _flow(block_number=102, log_index=1, token_delta=7, wallet=WALLET_B),
        _flow(block_number=103, log_index=1, token_delta=1, wallet=WALLET_B, token=TOKEN_Y),
    ]
    assert store.insert_flows(cur, flows) == 4
    written = store.refold(
        cur, [(WALLET_A, TOKEN_X), (WALLET_B, TOKEN_X), (WALLET_B, TOKEN_Y), (WALLET_A, TOKEN_Y)], _sum_fold
    )
    assert written == 3
    cur.execute("SELECT wallet, token, balance_token, flow_count, last_flow_block FROM positions_v2 ORDER BY 1, 2")
    assert cur.fetchall() == [
        (WALLET_A, TOKEN_X, Decimal(6), 2, 101),
        (WALLET_B, TOKEN_Y, Decimal(1), 1, 103),
        (WALLET_B, TOKEN_X, Decimal(7), 1, 102),
    ]
    cur.execute("SELECT block_number, basis_delta, realized_delta FROM wallet_flows ORDER BY 1")
    assert cur.fetchall() == [
        (100, Decimal(10), Decimal(-10)),
        (101, Decimal(-4), Decimal(4)),
        (102, Decimal(7), Decimal(-7)),
        (103, Decimal(1), Decimal(-1)),
    ]
    assert store.refold(cur, [(WALLET_A, TOKEN_X)], _sum_fold) == 1
    assert store.refold(cur, [(WALLET_A, TOKEN_Y)], _sum_fold) == 0
    assert store.refold(cur, [], _sum_fold) == 0
    assert store.refold(cur, [(WALLET_A, TOKEN_X)], lambda prev, fs: _State(sum(f.token_delta for f in fs))) == 1
    cur.execute(
        "SELECT balance_token, flow_count FROM positions_v2 WHERE wallet = %s AND token = %s", (WALLET_A, TOKEN_X)
    )
    assert cur.fetchone() == (Decimal(6), 1)


def test_refold_with_the_real_fold(cur):
    from core.ledger import store
    from core.ledger.fold import fold

    flows = [
        _flow(
            block_number=100,
            log_index=1,
            token_delta=10**18,
            quote_delta=-(2 * 10**18),
            mon_value=Decimal(2),
            kind="buy",
        ),
        _flow(
            block_number=101,
            log_index=1,
            token_delta=-(10**18),
            quote_delta=3 * 10**18,
            mon_value=Decimal(3),
            kind="sell",
            txhash=TX_2,
        ),
    ]
    assert store.insert_flows(cur, flows) == 2
    assert store.refold(cur, [(WALLET_A, TOKEN_X)], fold) == 1
    cur.execute(
        "SELECT balance_token, token_bought, token_sold, native_spent, native_received, realized_pnl_native, trade_count "
        "FROM positions_v2 WHERE wallet = %s AND token = %s",
        (WALLET_A, TOKEN_X),
    )
    row = cur.fetchone()
    assert [int(v) for v in row] == [0, 10**18, 10**18, 2 * 10**18, 3 * 10**18, 10**18, 2]


def test_tx_meta_cache(cur):
    from core.ledger import store
    from core.ledger.types import TxMeta

    metas = [
        TxMeta(
            txhash=TX_1.upper(),
            block_number=100,
            tx_index=4,
            from_addr=WALLET_A.upper(),
            to_addr=VENUE,
            value=BIG,
            selector="0xA9059CBB",
        ),
        TxMeta(txhash=TX_2, block_number=101, tx_index=0, from_addr=WALLET_B, to_addr=None, value=None, selector=None),
    ]
    store.put_tx_meta(cur, metas)
    out = store.get_tx_meta(cur, [TX_1, TX_2.upper(), "0xdeadbeef"])
    assert set(out) == {TX_1, TX_2}
    assert out[TX_1] == TxMeta(
        txhash=TX_1, block_number=100, tx_index=4, from_addr=WALLET_A, to_addr=VENUE, value=BIG, selector="0xa9059cbb"
    )
    assert isinstance(out[TX_1].value, int)
    assert out[TX_2] == metas[1]
    store.put_tx_meta(cur, [replace(metas[1], value=5), replace(metas[1], value=6)])
    assert store.get_tx_meta(cur, [TX_2])[TX_2].value == 6
    assert store.get_tx_meta(cur, []) == {}
    store.put_tx_meta(cur, [])


def test_trace_cache(cur):
    from core.ledger import store
    from core.ledger.types import TraceResult

    assert store.get_trace(cur, TX_1) is None
    trace = TraceResult(available=True, transfers=[(WALLET_A.upper(), VENUE, BIG), (VENUE, WALLET_B, 1)])
    store.put_trace(cur, TX_1.upper(), trace)
    loaded = store.get_trace(cur, TX_1)
    assert loaded.available is True
    assert loaded.transfers == [(WALLET_A, VENUE, BIG), (VENUE, WALLET_B, 1)]
    assert isinstance(loaded.transfers[0][2], int)
    store.put_trace(cur, TX_1, TraceResult(available=False, transfers=[]))
    loaded = store.get_trace(cur, TX_1)
    assert loaded.available is False
    assert loaded.transfers == []
    assert _count(cur, "tx_traces") == 1


def test_kinds_cache(cur):
    from core.ledger import store

    assert store.get_kinds(cur, [WALLET_A]) == {}
    store.put_kind(cur, WALLET_A.upper(), "contract_unknown", "getcode", 500, {"code": "0x60"})
    store.put_kind(cur, VENUE, "venue_curve", "known_list", None, None)
    assert store.get_kinds(cur, [WALLET_A, VENUE.upper(), WALLET_B]) == {
        WALLET_A: "contract_unknown",
        VENUE: "venue_curve",
    }
    store.put_kind(cur, WALLET_A, "venue_pool", "heuristic", 700, None)
    cur.execute("SELECT kind, source, first_seen_block, evidence FROM address_kinds WHERE address = %s", (WALLET_A,))
    assert cur.fetchone() == ("venue_pool", "heuristic", 500, {"code": "0x60"})
    store.put_kind(cur, WALLET_A, "venue_pool", "heuristic", 300, {"txs": 2})
    cur.execute("SELECT first_seen_block, evidence FROM address_kinds WHERE address = %s", (WALLET_A,))
    assert cur.fetchone() == (300, {"txs": 2})
    store.put_kind(cur, VENUE, "venue_custody", "manual", 900, None)
    cur.execute("SELECT first_seen_block FROM address_kinds WHERE address = %s", (VENUE,))
    assert cur.fetchone() == (900,)
    assert store.get_kinds(cur, []) == {}


def test_upsert_venue(cur):
    from core.ledger import store

    store.upsert_venue(cur, VENUE.upper(), "venue_pool", discovered=True, evidence={"txs": 2})
    cur.execute("SELECT kind, token0, token1, discovered, evidence FROM venues WHERE address = %s", (VENUE,))
    assert cur.fetchone() == ("venue_pool", None, None, True, {"txs": 2})
    store.upsert_venue(cur, VENUE, "venue_pool", token0=TOKEN_X.upper(), token1=None)
    cur.execute("SELECT kind, token0, token1, discovered, evidence FROM venues WHERE address = %s", (VENUE,))
    assert cur.fetchone() == ("venue_pool", TOKEN_X, None, False, {"txs": 2})
    store.upsert_venue(cur, VENUE, "venue_curve", token1=TOKEN_Y, discovered=True)
    cur.execute("SELECT kind, token0, token1, discovered FROM venues WHERE address = %s", (VENUE,))
    assert cur.fetchone() == ("venue_curve", TOKEN_X, TOKEN_Y, False)
    assert _count(cur, "venues") == 1


def test_registry_is_cached_until_refreshed(cur):
    from core.ledger import store
    from core.ledger.types import TokenReg

    assert store.registry(cur) == {}
    reg = store.register_token(cur, TOKEN_X.upper(), "crystal", 1000, "native", None)
    assert reg == TokenReg(
        token=TOKEN_X, source="crystal", registered_block=1000, quote_token="native", decimals=18, active=True
    )
    assert store.registry(cur) == {TOKEN_X: reg}
    cur.execute(
        "INSERT INTO token_registry (token, source, registered_block, quote_token, decimals) VALUES (%s, 'nadfun_v2', 5, %s, 6)",
        (TOKEN_Y, "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"),
    )
    assert TOKEN_Y not in store.registry(cur)
    assert set(store.registry(cur, refresh=True)) == {TOKEN_X, TOKEN_Y}
    assert store.registry(cur)[TOKEN_Y].decimals == 6
    assert set(store.refresh_registry(cur)) == {TOKEN_X, TOKEN_Y}
    updated = store.register_token(cur, TOKEN_X, "spot_base", 2000, None, 9)
    assert updated == TokenReg(
        token=TOKEN_X, source="spot_base", registered_block=1000, quote_token="native", decimals=9, active=True
    )
    assert store.registry(cur)[TOKEN_X] == updated
    earlier = store.register_token(cur, TOKEN_X, "spot_base", 500, "0x754704bc059f8c67012fed69bc8a327a5aafb603", 9)
    assert earlier.registered_block == 500
    assert earlier.quote_token == "0x754704bc059f8c67012fed69bc8a327a5aafb603"
    store.invalidate_registry()
    assert store.registry(cur)[TOKEN_X] == earlier


def test_ledger_meta_round_trip(cur):
    from core.ledger import store

    assert store.get_ledger_meta(cur, "head") is None
    store.set_ledger_meta(cur, "head", 12345)
    assert store.get_ledger_meta(cur, "head") == "12345"
    store.set_ledger_meta(cur, "head", "12346")
    assert store.get_ledger_meta(cur, "head") == "12346"
    store.set_ledger_meta(cur, "head", None)
    assert store.get_ledger_meta(cur, "head") is None
