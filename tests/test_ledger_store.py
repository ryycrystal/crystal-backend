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
        assert head.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS", "CREATE OR REPLACE VIEW"))


def _coverage(cur):
    cur.execute("SELECT token, from_block, to_block FROM token_coverage ORDER BY token, from_block")
    return cur.fetchall()


def _from_creation(cur, token):
    cur.execute(
        "SELECT from_block, to_block FROM ledger_token_coverage WHERE token = %s AND from_creation ORDER BY 1", (token,)
    )
    return cur.fetchall()


def test_coverage_ranges_merge_when_they_touch_and_stay_apart_when_they_do_not(cur):
    from core.ledger import store

    cur.execute("DELETE FROM token_coverage")
    store.extend_coverage(cur, TOKEN_X, 100, 200)
    store.extend_coverage(cur, TOKEN_X, 201, 250)
    store.extend_coverage(cur, TOKEN_X, 150, 260)
    store.extend_coverage(cur, TOKEN_X, 300, 400)
    store.extend_coverage(cur, TOKEN_X, 120, 130)
    assert _coverage(cur) == [(TOKEN_X, 100, 260), (TOKEN_X, 300, 400)]
    store.extend_coverage(cur, TOKEN_X, 261, 299)
    assert _coverage(cur) == [(TOKEN_X, 100, 400)]
    store.extend_coverage(cur, TOKEN_X, 500, 499)
    assert _coverage(cur) == [(TOKEN_X, 100, 400)]
    store.extend_coverage(cur, None, 90, 95)
    store.extend_coverage(cur, None, 96, 96)
    assert _coverage(cur) == [("*", 90, 96), (TOKEN_X, 100, 400)]


def test_coverage_from_creation_needs_the_registered_block_inside_one_unbroken_span(cur):
    from core.ledger import store

    cur.execute("DELETE FROM token_coverage")
    cur.execute("DELETE FROM token_registry")
    store.register_token(cur, TOKEN_X, "crystal", 1000, None, 18)
    store.register_token(cur, TOKEN_Y, "crystal", 5000, None, 18)
    store.register_token(cur, "0x" + "77" * 20, "spot_quote", None, None, 18)
    store.extend_coverage(cur, TOKEN_X, 1200, 2000)
    assert store.coverage_from_creation(cur, [TOKEN_X, TOKEN_Y]) == {}
    store.extend_coverage(cur, TOKEN_X, 1000, 1199)
    assert store.coverage_from_creation(cur, [TOKEN_X, TOKEN_Y]) == {TOKEN_X: 2000}
    store.extend_coverage(cur, None, 4000, 6000)
    assert store.coverage_from_creation(cur, [TOKEN_X, TOKEN_Y]) == {TOKEN_X: 2000, TOKEN_Y: 6000}
    store.extend_coverage(cur, None, 2001, 3999)
    assert store.coverage_from_creation(cur, [TOKEN_X, TOKEN_Y]) == {TOKEN_X: 6000, TOKEN_Y: 6000}
    assert _from_creation(cur, TOKEN_X) == [(1000, 6000)]
    unknown = "0x" + "77" * 20
    store.extend_coverage(cur, unknown, 100, 9000)
    assert store.coverage_from_creation(cur, [unknown]) == {}, "an unknown creation block cannot be certified covered"
    assert store.coverage_from_creation(cur, []) == {}
    cur.execute("DELETE FROM token_coverage")
    cur.execute("DELETE FROM token_registry")
    store.invalidate_registry()


def test_delete_token_flows_removes_one_block_of_the_named_tokens_and_names_the_positions(cur):
    from core.ledger import store

    cur.execute("DELETE FROM wallet_flows")
    flows = [
        _flow(block_number=100, log_index=1),
        _flow(block_number=100, log_index=2, wallet=WALLET_B),
        _flow(block_number=100, log_index=3, token=TOKEN_Y),
        _flow(block_number=101, log_index=1),
    ]
    assert store.insert_flows(cur, flows) == 4
    gone = store.delete_token_flows(cur, [TOKEN_X.upper()], 100)
    assert sorted(gone) == [(WALLET_A, TOKEN_X), (WALLET_B, TOKEN_X)]
    assert store.delete_token_flows(cur, [], 100) == []
    cur.execute("SELECT block_number, token FROM wallet_flows ORDER BY 1, 2")
    assert cur.fetchall() == [(100, TOKEN_Y), (101, TOKEN_X)]


def test_ledger_schema_widens_tables_an_earlier_version_created(cur):
    from core.ledger.schema import ADDED_COLUMNS, init_ledger_schema, missing_columns
    from core.ledger.store import FLOW_COLUMNS, POSITION_COLUMNS

    cur.execute("DROP TABLE IF EXISTS positions_v2")
    cur.execute("DROP TABLE IF EXISTS wallet_flows")
    cur.execute("CREATE TABLE positions_v2 (wallet TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY (wallet, token))")
    cur.execute(
        "CREATE TABLE wallet_flows (block_number BIGINT NOT NULL, tx_index INTEGER NOT NULL, "
        "log_index INTEGER NOT NULL, sub_index INTEGER NOT NULL, wallet TEXT NOT NULL, token TEXT NOT NULL, "
        "PRIMARY KEY (block_number, tx_index, log_index, sub_index))"
    )
    cur.execute("INSERT INTO positions_v2 (wallet, token) VALUES ('a', 'b')")
    assert all(missing_columns(cur, table, ddl) for table, ddl in ADDED_COLUMNS)
    init_ledger_schema(cur)
    assert not any(missing_columns(cur, table, ddl) for table, ddl in ADDED_COLUMNS)
    cur.execute("SELECT observed_tokens, last_trade_tx FROM positions_v2 WHERE wallet = 'a'")
    assert cur.fetchone() == (0, None)
    cur.execute("DROP TABLE positions_v2")
    cur.execute("DROP TABLE wallet_flows")
    init_ledger_schema(cur)
    for table, columns in (("positions_v2", POSITION_COLUMNS), ("wallet_flows", FLOW_COLUMNS)):
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        present = {row[0] for row in cur.fetchall()}
        assert set(columns) <= present, (table, set(columns) - present)


def test_insert_flows_keeps_the_stored_reading_and_collapses_duplicates_within_one_batch(cur):
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
    """A stand-in for the real fold: one state per wallet, resuming from prev when it is given one."""
    by_wallet: dict[str, list] = {}
    for flow in flows:
        by_wallet.setdefault(flow.wallet, []).append(flow)
    states = {}
    for wallet, wallet_flows in by_wallet.items():
        carried = prev.get(wallet) if prev else None
        states[wallet] = _position(
            wallet=wallet,
            token=wallet_flows[0].token,
            balance_token=sum(f.token_delta for f in wallet_flows) + (carried.balance_token if carried else 0),
            flow_count=len(wallet_flows) + (carried.flow_count if carried else 0),
            first_flow_ts=wallet_flows[0].timestamp,
            last_flow_ts=wallet_flows[-1].timestamp,
            last_flow_block=wallet_flows[-1].block_number,
        )
    return states, [replace(f, basis_delta=f.token_delta, realized_delta=-f.token_delta) for f in flows]


def test_refold_round_trip_writes_positions_and_fold_deltas(cur):
    from core.ledger import store

    flows = [
        _flow(block_number=100, log_index=1, token_delta=10),
        _flow(block_number=101, log_index=1, token_delta=-4),
        _flow(block_number=102, log_index=1, token_delta=7, wallet=WALLET_B),
        _flow(block_number=103, log_index=1, token_delta=1, wallet=WALLET_B, token=TOKEN_Y),
    ]
    assert store.insert_flows(cur, flows) == 4
    written = store.refold_tokens(cur, {TOKEN_X: 0, TOKEN_Y: 0}, _sum_fold)
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
    assert store.refold_tokens(cur, {TOKEN_X: 0}, _sum_fold) == 2
    assert store.refold_tokens(cur, {"0x" + "99" * 20: 0}, _sum_fold) == 0
    assert store.refold_tokens(cur, {}, _sum_fold) == 0


def test_refold_with_the_real_fold(cur):
    from core.ledger import store
    from core.ledger.fold import fold_token

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
    assert store.refold_tokens(cur, {TOKEN_X: 0}, fold_token) == 1
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


def test_purge_wallets_removes_flows_and_positions_of_the_named_wallets_only(cur):
    from core.ledger import store

    store.insert_flows(
        cur,
        [
            _flow(wallet=WALLET_A, log_index=1),
            _flow(wallet=WALLET_A, log_index=2, token=TOKEN_Y),
            _flow(wallet=WALLET_B, log_index=3),
        ],
    )
    store.upsert_positions(cur, [_position(wallet=WALLET_A), _position(wallet=WALLET_B)])

    assert store.purge_wallets(cur, [WALLET_A.upper(), WALLET_A]) == sorted({TOKEN_X, TOKEN_Y}), (
        "the tokens it names are the ones whose fold is now stale"
    )
    assert store.purge_wallets(cur, []) == []

    cur.execute("SELECT DISTINCT wallet FROM wallet_flows")
    assert {r[0] for r in cur.fetchall()} == {WALLET_B}
    cur.execute("SELECT wallet FROM positions_v2")
    assert [r[0] for r in cur.fetchall()] == [WALLET_B]


def _buy(block, wallet, amount, cost, log_index=1):
    return _flow(
        block_number=block,
        log_index=log_index,
        wallet=wallet,
        token=TOKEN_X,
        token_delta=amount,
        quote_delta=-cost,
        mon_value=Decimal(cost) / Decimal(10**18),
        kind="buy",
        txhash="0x" + f"{block:064x}",
    )


def _handover(block, sender, receiver, amount, log_index=5):
    common = dict(
        block_number=block,
        log_index=log_index,
        token=TOKEN_X,
        quote_asset=None,
        quote_delta=None,
        mon_value=Decimal(0),
        source="transfer_net",
        venue=None,
        txhash="0x" + f"{block:064x}",
    )
    return [
        _flow(sub_index=0, wallet=sender, token_delta=-amount, kind="transfer_out", counterparty=receiver, **common),
        _flow(sub_index=1, wallet=receiver, token_delta=amount, kind="transfer_in", counterparty=sender, **common),
    ]


def _covered(cur, token, through):
    from core.ledger import store

    store.register_token(cur, token, "crystal", 1, None, 18)
    store.extend_coverage(cur, token, 1, through)


def test_folding_a_token_incrementally_matches_folding_it_whole(cur):
    from core.ledger import store
    from core.ledger.fold import fold_token

    _covered(cur, TOKEN_X, 400)
    flows = [
        _buy(100, WALLET_A, 10**18, 2 * 10**18),
        *_handover(200, WALLET_A, WALLET_B, 4 * 10**17),
        _flow(
            block_number=300,
            log_index=1,
            wallet=WALLET_B,
            token=TOKEN_X,
            token_delta=-(4 * 10**17),
            quote_delta=10**18,
            mon_value=Decimal(1),
            kind="sell",
            txhash=TX_2,
        ),
    ]
    assert store.insert_flows(cur, flows) == 4
    assert store.refold_tokens(cur, {TOKEN_X: 100}, fold_token) == 2
    cur.execute("SELECT folded_through FROM token_fold_state WHERE token = %s", (TOKEN_X,))
    assert cur.fetchone()[0] == 300
    whole = _positions(cur, TOKEN_X)

    cur.execute("TRUNCATE positions_v2, parked_entitlements, token_fold_state")
    for block in (100, 200, 300):
        touched = min(f.block_number for f in flows if f.block_number == block)
        assert store.refold_tokens(cur, {TOKEN_X: touched}, fold_token) >= 1
    assert _positions(cur, TOKEN_X) == whole, "one flush per block must land where a single pass lands"


def _positions(cur, token):
    cur.execute(
        "SELECT wallet, balance_token, cost_basis_native, realized_pnl_native, unresolved_tokens, flow_count "
        "FROM positions_v2 WHERE token = %s ORDER BY wallet",
        (token,),
    )
    return cur.fetchall()


def test_a_flow_below_the_watermark_forces_the_whole_token_to_be_folded_again(cur):
    from core.ledger import store
    from core.ledger.fold import fold_token

    _covered(cur, TOKEN_X, 400)
    store.insert_flows(cur, [_buy(300, WALLET_A, 10**18, 2 * 10**18)])
    assert store.refold_tokens(cur, {TOKEN_X: 300}, fold_token) == 1
    assert _positions(cur, TOKEN_X) == [(WALLET_A, Decimal(10**18), Decimal(2 * 10**18), Decimal(0), Decimal(0), 1)]

    store.insert_flows(cur, [_buy(100, WALLET_A, 3 * 10**18, 3 * 10**18, log_index=2)])
    assert store.refold_tokens(cur, {TOKEN_X: 100}, fold_token) == 1
    assert _positions(cur, TOKEN_X) == [
        (WALLET_A, Decimal(4 * 10**18), Decimal(5 * 10**18), Decimal(0), Decimal(0), 2)
    ], "the earlier flow must be folded in, not appended on top of a stale checkpoint"


def test_folding_a_token_forgets_positions_whose_flows_are_gone(cur):
    from core.ledger import store
    from core.ledger.fold import fold_token

    _covered(cur, TOKEN_X, 400)
    store.insert_flows(cur, [_buy(100, WALLET_A, 10**18, 10**18), _buy(101, WALLET_B, 10**18, 10**18)])
    assert store.refold_tokens(cur, {TOKEN_X: 100}, fold_token) == 2
    cur.execute("DELETE FROM wallet_flows WHERE wallet = %s", (WALLET_B,))
    assert store.refold_tokens(cur, {TOKEN_X: 0}, fold_token) == 1
    assert [row[0] for row in _positions(cur, TOKEN_X)] == [WALLET_A]
    cur.execute("DELETE FROM wallet_flows")
    assert store.refold_tokens(cur, {TOKEN_X: 0}, fold_token) == 0
    assert _positions(cur, TOKEN_X) == []


def test_a_newer_interpretation_of_the_same_movement_replaces_the_older_reading(cur):
    """Evidence is fixed; what the engine made of it is not. A re-replay must correct, not be ignored."""
    from core.ledger import store
    from core.ledger.types import INTERPRETATION

    original = _flow(kind="buy", token_delta=10**18, interpretation=INTERPRETATION)
    assert store.insert_flows(cur, [original]) == 1

    same_reading = replace(original, kind="sell", token_delta=-(10**18))
    assert store.insert_flows(cur, [same_reading]) == 0, "no newer reading, so nothing changes"
    cur.execute("SELECT kind FROM wallet_flows")
    assert cur.fetchone()[0] == "buy"

    newer = replace(original, kind="sell", token_delta=-(10**18), interpretation=INTERPRETATION + 1)
    assert store.insert_flows(cur, [newer]) == 1
    cur.execute("SELECT kind, token_delta, interpretation FROM wallet_flows")
    assert cur.fetchone() == ("sell", Decimal(-(10**18)), INTERPRETATION + 1)

    stale = replace(original, kind="buy", interpretation=INTERPRETATION - 1)
    assert store.insert_flows(cur, [stale]) == 0
    cur.execute("SELECT kind FROM wallet_flows")
    assert cur.fetchone()[0] == "sell", "an older reading never wins"
    assert _count(cur, "wallet_flows") == 1


def test_a_position_resumed_from_the_database_counts_in_whole_wei(cur):
    """A numeric column reads back as a decimal, and decimal division truncates where integer division floors."""
    from core.ledger import store
    from core.ledger.fold import fold_token

    _covered(cur, TOKEN_X, 400)
    store.insert_flows(cur, [_buy(100, WALLET_A, 3 * 10**18, 10**18)])
    store.refold_tokens(cur, {TOKEN_X: 100}, fold_token)
    resumed = store.load_positions(cur, TOKEN_X, [WALLET_A])[WALLET_A]
    for name in ("balance_token", "cost_basis_native", "observed_tokens", "unresolved_tokens"):
        assert isinstance(getattr(resumed, name), int), name
    assert resumed.balance_token == 3 * 10**18 and resumed.cost_basis_native == 10**18


def _park(block, wallet, amount, venue, kind="vault_deposit", log_index=1):
    return _flow(
        block_number=block,
        log_index=log_index,
        wallet=wallet,
        token=TOKEN_X,
        token_delta=amount,
        quote_asset=None,
        quote_delta=None,
        mon_value=Decimal(0),
        kind=kind,
        counterparty=venue,
        venue=venue,
        source="transfer_net",
        txhash="0x" + f"{block:064x}",
    )


def test_parked_basis_survives_a_checkpoint_that_goes_through_the_database(cur):
    """Resuming must restore which vault held what, or a later withdrawal is priced off the wrong one."""
    from core.ledger import store
    from core.ledger.fold import fold_token

    vault_a, vault_b = "0x" + "a5" * 20, "0x" + "b6" * 20
    _covered(cur, TOKEN_X, 900)
    store.insert_flows(
        cur,
        [
            _buy(100, WALLET_A, 200, 200),
            _park(200, WALLET_A, -100, vault_a),
            _park(300, WALLET_A, -100, vault_b, log_index=2),
        ],
    )
    assert store.refold_tokens(cur, {TOKEN_X: 100}, fold_token) == 1
    parked = store.load_parked(cur, [(WALLET_A, TOKEN_X)])[(WALLET_A, TOKEN_X)]
    assert {venue: bucket.observed_tokens for venue, bucket in parked.items()} == {vault_a: 100, vault_b: 100}

    store.insert_flows(cur, [_park(400, WALLET_A, 100, vault_a, kind="vault_withdraw")])
    assert store.refold_tokens(cur, {TOKEN_X: 400}, fold_token) == 1
    cur.execute("SELECT basis_delta FROM wallet_flows WHERE block_number = 400")
    assert cur.fetchone()[0] == Decimal(100), "the withdrawal takes back what vault A held, resumed from storage"
    cur.execute("SELECT venue, observed_tokens FROM parked_entitlements WHERE wallet = %s", (WALLET_A,))
    assert cur.fetchall() == [(vault_b, Decimal(100))], "vault A is emptied and forgotten, vault B untouched"
    cur.execute("SELECT parked_observed_basis, cost_basis_native FROM positions_v2 WHERE wallet = %s", (WALLET_A,))
    assert cur.fetchone() == (Decimal(100), Decimal(100))
