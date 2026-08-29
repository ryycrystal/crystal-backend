"""Database-backed resilience tests for the native launchpad.

These run against a real Postgres because the mocked-storage suite cannot see
persistence behaviour -- it already missed curve reserves being written on only
one of the two write paths.

Run with:

    TEST_DATABASE_URL="postgresql://user:pass@localhost:5432/postgres?sslmode=disable" \
        python -m pytest tests/test_launchpad_integration.py -q

The URL must point at an existing database on the target server (usually
``postgres``); a scratch database is created and dropped around the module.
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL to run database integration tests")

SCRATCH_DB = "crystal_lp_itest"

ROUTER = None
TOKEN = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
TOKEN_B = "0x2c6dd544e63cae0330b5edc5f0bcd108652c8888"
CREATOR = "0x77d4d8e13b228e474b1c53d6adeebef4dfa51603"
USER = "0x1234567890abcdef1234567890abcdef12345678"
MARKET = "0x975c4885538ba5072c66f48d4c4c7253e388c3e0"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

INITIAL_TOKEN_SUPPLY = 10**27
GRADUATED_TOKEN_SUPPLY = 2 * 10**26
CURVE_SUPPLY = INITIAL_TOKEN_SUPPLY - GRADUATED_TOKEN_SUPPLY
V0 = 1000 * 10**18
K = V0 * INITIAL_TOKEN_SUPPLY

LAUNCHPAD_TABLES = (
    "launchpad_trades",
    "launchpad_positions",
    "launchpad_snipers",
    "launchpad_users",
    "launchpad_tokens",
    "launchpad_blocks",
)


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

    admin_url = RAW_URL
    scratch_url = _swap_db(RAW_URL, SCRATCH_DB)

    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{SCRATCH_DB}';")
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB};")
        cur.execute(f"CREATE DATABASE {SCRATCH_DB};")
    conn.close()

    from core.storage import base as storage_base

    storage_base._DATABASE_URL = scratch_url
    storage_base._POOL = None

    import core.storage as storage

    storage.init_pool()
    storage.init_db()

    yield scratch_url

    try:
        storage_base.close_pool()
    except Exception:
        pass
    storage_base._POOL = None
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{SCRATCH_DB}';")
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB};")
    conn.close()


@pytest.fixture(autouse=True)
def clean(db):
    import psycopg2

    conn = psycopg2.connect(db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(LAUNCHPAD_TABLES) + " RESTART IDENTITY CASCADE;")
    conn.close()
    yield


def _ta(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _w(v: int) -> str:
    return f"{v:064x}"


def _string_tail(value: str) -> str:
    raw = value.encode()
    padded = ((len(raw) + 31) // 32) * 32
    return _w(len(raw)) + raw.hex().ljust(padded * 2, "0")


def _tc_data(strings) -> str:
    tails = [_string_tail(s) for s in strings]
    offset = len(strings) * 32
    heads = []
    for t in tails:
        heads.append(_w(offset))
        offset += len(t) // 2
    return "".join(heads) + "".join(tails)


def _lt_data(is_buy, amount_in, amount_out, native_reserve, token_reserve) -> str:
    return _w(1 if is_buy else 0) + _w(amount_in) + _w(amount_out) + _w(native_reserve) + _w(token_reserve)


def _reserve_for(native_reserve: int) -> int:
    return (K + native_reserve - 1) // native_reserve


def _router():
    from core import chain as h

    return h.CONTRACTS["ROUTER"].lower()


def _new_state():
    import state

    state._LAUNCHPAD_PARAMS_CACHE["initial_native_supply"] = V0
    return state.State()


def _create(st, token=TOKEN, blk=100, ts=1000, name="Tok", symbol="TOK"):
    from modules import launchpad as lp_mod

    ev = lp_mod.parse_token_created(
        _router(),
        ["0x", _ta(token), _ta(CREATOR)],
        _tc_data([name, symbol, "cid", "desc", "", "", "", ""]),
    )
    st.apply_token_created(blk, ev, ts, _router())
    return ev


def _trade(
    st,
    token=TOKEN,
    native_reserve=1500 * 10**18,
    blk=101,
    ts=1001,
    txh="0xaa",
    log_idx=0,
    is_buy=True,
    amount_in=10**18,
    amount_out=10**20,
):
    from modules import launchpad as lp_mod

    ev = lp_mod.parse_launchpad_trade(
        _router(),
        ["0x", _ta(token), _ta(USER)],
        _lt_data(is_buy, amount_in, amount_out, native_reserve, _reserve_for(native_reserve)),
    )
    st.apply_launchpad_trade(ev, blk, ts, txh, log_idx, _router())
    return ev


def _market_created_ev(token=TOKEN, market=MARKET, market_id=1):
    return {
        "isCanonical": True,
        "marketType": 3,
        "market": market,
        "quoteAsset": WMON,
        "baseAsset": token,
        "quoteAddress": WMON,
        "baseAddress": token,
        "quoteDecimals": 18,
        "baseDecimals": 18,
        "quoteTicker": "WMON",
        "baseTicker": "TOK",
        "quoteName": "Wrapped Monad",
        "baseName": "Tok",
        "marketId": market_id,
        "scaleFactor": 9,
        "tickSize": 1,
        "maxPrice": 10**15,
        "minSize": 1,
        "takerFee": 99910,
        "makerRebate": 99995,
    }


def _q(db, sql, args=None):
    import psycopg2

    conn = psycopg2.connect(db)
    with conn.cursor() as cur:
        cur.execute(sql, args or ())
        rows = cur.fetchall()
    conn.close()
    return rows


def _x(db, sql, args=None):
    import psycopg2

    conn = psycopg2.connect(db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql, args or ())
    conn.close()


def _token_row(db, token=TOKEN):
    return _q(
        db,
        """
        SELECT circulating_supply, approaching_75, curve_native_reserve,
               curve_token_reserve, native_volume, tx_count, migrated, market
        FROM launchpad_tokens WHERE token = %s
        """,
        (token,),
    )[0]


def test_duplicate_log_delivery_is_idempotent(db):
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, txh="0xdup", log_idx=0)
    before = _token_row(db)
    trades_before = _q(db, "SELECT count(*) FROM launchpad_trades")[0][0]

    st2 = _new_state()
    st2.rebuild_from_db()
    _trade(st2, native_reserve=2500 * 10**18, txh="0xdup", log_idx=0)

    assert _q(db, "SELECT count(*) FROM launchpad_trades")[0][0] == trades_before
    assert _token_row(db) == before


def test_duplicate_tx_with_multiple_logs(db):
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=1500 * 10**18, txh="0xmulti", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, txh="0xmulti", log_idx=1)
    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xmulti'")[0][0] == 2

    st2 = _new_state()
    st2.rebuild_from_db()
    _trade(st2, native_reserve=1500 * 10**18, txh="0xmulti", log_idx=0)
    _trade(st2, native_reserve=2000 * 10**18, txh="0xmulti", log_idx=1)
    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xmulti'")[0][0] == 2


def test_trade_before_creation_is_recovered_and_backfilled(db):
    """Out-of-order delivery: the trade arrives before TokenCreated."""
    import state

    st = _new_state()
    orig = state._fetch_token_string
    state._fetch_token_string = lambda tok, sel: ""
    try:
        _trade(st, native_reserve=2500 * 10**18, txh="0xooo", log_idx=0)
        assert _q(db, "SELECT count(*) FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0] == 1
        assert _q(db, "SELECT count(*) FROM launchpad_trades")[0][0] == 1

        _create(st, blk=102, ts=1002)
        creator = _q(db, "SELECT creator FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0]
        assert creator == CREATOR
    finally:
        state._fetch_token_string = orig


def test_created_to_active_on_first_trade(db):
    from core.adapters import native as native_mod
    from core.lifecycle import TokenPhase, resolve_phase

    st = _new_state()
    _create(st)
    row = _token_row(db)
    assert row[5] == 0
    assert resolve_phase(curve=None, has_trades=False) is TokenPhase.CREATED

    ev = _trade(st, native_reserve=1500 * 10**18, txh="0xact", log_idx=0)
    row = _token_row(db)
    assert row[5] == 1

    curve = native_mod.NativeLaunchpadAdapter().curve_state(ev)
    assert resolve_phase(curve=curve, has_trades=True) is TokenPhase.ACTIVE


def test_repeated_graduation_events_are_stable(db):
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, txh="0xg", log_idx=0)

    for _ in range(3):
        st.apply_migrated(102, 1002, {"token": TOKEN}, _router())
        st.apply_market_created(102, 1002, _market_created_ev(), _router())

    row = _token_row(db)
    assert row[6] is True
    assert row[7] == MARKET
    assert st.launchpad_market_to_token[MARKET] == TOKEN


def test_graduation_partially_processed_then_replayed(db):
    """Migrated lands, MarketCreated is lost, then the whole tx is replayed."""
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, txh="0xpart", log_idx=0)

    st.apply_migrated(102, 1002, {"token": TOKEN}, _router())
    assert _token_row(db)[6] is True
    assert _token_row(db)[7] in (None, "")

    st2 = _new_state()
    st2.rebuild_from_db()
    st2.apply_migrated(102, 1002, {"token": TOKEN}, _router())
    st2.apply_market_created(102, 1002, _market_created_ev(), _router())

    row = _token_row(db)
    assert row[6] is True
    assert row[7] == MARKET


def test_restart_from_checkpoint_reproduces_state(db):
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, txh="0xr", log_idx=0)
    persisted = _token_row(db)

    st2 = _new_state()
    st2.rebuild_from_db()
    lp = st2.launchpad_tokens[TOKEN]

    assert int(lp.circulating_supply) == int(persisted[0])
    assert bool(lp.approaching_75) == bool(persisted[1])
    assert int(lp.curve_native_reserve) == int(persisted[2])
    assert int(lp.curve_token_reserve) == int(persisted[3])
    assert int(lp.native_volume) == int(persisted[4])
    assert int(lp.tx_count) == int(persisted[5])


def test_replaying_same_range_produces_no_changes(db):
    st = _new_state()
    _create(st)
    for i, nr in enumerate((1500 * 10**18, 2000 * 10**18, 2500 * 10**18)):
        _trade(st, native_reserve=nr, blk=101 + i, ts=1001 + i, txh=f"0xrep{i}", log_idx=0)
    snapshot = _token_row(db)
    trades = _q(db, "SELECT txhash, log_index, native_amount FROM launchpad_trades ORDER BY txhash, log_index")

    st2 = _new_state()
    st2.rebuild_from_db()
    _create(st2)
    for i, nr in enumerate((1500 * 10**18, 2000 * 10**18, 2500 * 10**18)):
        _trade(st2, native_reserve=nr, blk=101 + i, ts=1001 + i, txh=f"0xrep{i}", log_idx=0)

    assert _token_row(db) == snapshot
    assert _q(db, "SELECT txhash, log_index, native_amount FROM launchpad_trades ORDER BY txhash, log_index") == trades


def test_multiple_tokens_do_not_cross_contaminate(db):
    st = _new_state()
    _create(st, token=TOKEN, name="A", symbol="A")
    _create(st, token=TOKEN_B, name="B", symbol="B")

    _trade(st, token=TOKEN, native_reserve=2500 * 10**18, txh="0xa", log_idx=0)
    _trade(st, token=TOKEN_B, native_reserve=1500 * 10**18, txh="0xb", log_idx=0)

    a = _token_row(db, TOKEN)
    b = _token_row(db, TOKEN_B)
    assert a[0] == 600_000_000 and a[1] is True
    assert b[0] < 600_000_000 and b[1] is False
    assert a[2] != b[2]


def test_transaction_rollback_leaves_no_partial_rows(db):
    """A failure inside an explicit transaction must not persist partial work."""
    import psycopg2

    conn = psycopg2.connect(db)
    try:
        with conn.cursor() as cur:
            st = _new_state()
            ev = _tc_ev()
            st.apply_token_created(100, ev, 1000, _router(), cur=cur)
            raise RuntimeError("boom mid-block")
    except RuntimeError:
        conn.rollback()
    finally:
        conn.close()

    assert _q(db, "SELECT count(*) FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0] == 0


def _tc_ev():
    from modules import launchpad as lp_mod

    return lp_mod.parse_token_created(
        _router(),
        ["0x", _ta(TOKEN), _ta(CREATOR)],
        _tc_data(["Tok", "TOK", "cid", "desc", "", "", "", ""]),
    )


def test_crash_midway_through_block_then_reprocess(db):
    """First log of a block commits, second is lost, block is reprocessed."""
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=1500 * 10**18, blk=105, ts=1005, txh="0xcrash", log_idx=0)

    st2 = _new_state()
    st2.rebuild_from_db()
    _trade(st2, native_reserve=1500 * 10**18, blk=105, ts=1005, txh="0xcrash", log_idx=0)
    _trade(st2, native_reserve=2500 * 10**18, blk=105, ts=1005, txh="0xcrash", log_idx=1)

    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xcrash'")[0][0] == 2
    row = _token_row(db)
    assert int(row[2]) == 2500 * 10**18


def test_sniper_count_does_not_exceed_distinct_snipers(db):
    """One address buying twice inside the window is ONE sniper.

    Must run through the BatchAccumulator: that is the path production uses, and
    it was the one incrementing the counter unconditionally while the table
    deduped, so snipers_count drifted above the row count.
    """
    import psycopg2

    from core.sequencer import BatchAccumulator
    from modules import launchpad as lp_mod

    st = _new_state()
    _create(st, blk=100, ts=1000)

    batch = BatchAccumulator()
    conn = psycopg2.connect(db)
    try:
        with conn.cursor() as cur:
            for i, nr in enumerate((1100 * 10**18, 1200 * 10**18)):
                ev = lp_mod.parse_launchpad_trade(
                    _router(),
                    ["0x", _ta(TOKEN), _ta(USER)],
                    _lt_data(True, 10**18, 10**20, nr, _reserve_for(nr)),
                )
                st.apply_launchpad_trade(ev, 101 + i, 1001 + i, f"0xsnipe{i}", 0, _router(), cur=cur, batch=batch)
            batch.flush(cur)
        conn.commit()
    finally:
        conn.close()

    rows = _q(db, "SELECT count(*) FROM launchpad_snipers WHERE token=%s", (TOKEN,))[0][0]
    counted = _q(db, "SELECT snipers_count FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0]
    assert rows == 1, "one address is one sniper"
    assert int(counted) == rows, f"snipers_count {counted} must equal distinct snipers {rows}"


def test_ath_is_persisted_and_never_regresses(db):
    """ATH derived client-side from loaded bars only sees the fetched window and
    resets on reload, so it lives in the index. GREATEST also makes it replay-
    safe: reprocessing a trade can never lower it."""
    st = _new_state()
    _create(st, blk=100, ts=1000)

    def ath():
        return _q(db, "SELECT ath_price_native FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0]

    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xa", log_idx=0)
    first = ath()
    assert first > 0, "a trade must establish an ATH"

    _trade(st, native_reserve=2000 * 10**18, blk=102, ts=1002, txh="0xb", log_idx=0)
    peak = ath()
    assert peak > first

    _trade(st, native_reserve=1200 * 10**18, blk=103, ts=1003, txh="0xc", log_idx=0)
    current = _q(db, "SELECT last_price_native FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0]
    assert ath() == peak, "ATH must not follow price down"
    assert current < peak

    _trade(st, native_reserve=2000 * 10**18, blk=102, ts=1002, txh="0xb", log_idx=0)
    assert ath() == peak


def test_ath_backfills_from_existing_trades_on_migration(db):
    """Tokens indexed before the column existed must not report an ATH of 0."""
    import core.storage as storage

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=2000 * 10**18, blk=101, ts=1001, txh="0xa", log_idx=0)
    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=1002, txh="0xb", log_idx=0)

    peak = _q(db, "SELECT MAX(price_native) FROM launchpad_trades WHERE token=%s", (TOKEN,))[0][0]

    _x(db, "UPDATE launchpad_tokens SET ath_price_native = 0 WHERE token=%s", (TOKEN,))
    assert _q(db, "SELECT ath_price_native FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0] == 0

    storage.init_db()

    assert _q(db, "SELECT ath_price_native FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0] == peak


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.api  # noqa: F401
    from api.routes.launchpad import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_trades_range_endpoint_spans_history_and_filters(db):
    """Chart marks need more history than the fixed 50-trade window returns."""
    st = _new_state()
    _create(st, blk=100, ts=1000)
    for i in range(12):
        _trade(st, native_reserve=(1100 + i * 50) * 10**18, blk=101 + i, ts=1000 + i * 60, txh=f"0xr{i}", log_idx=0)

    c = _api_client()

    body = c.get(f"/token/{TOKEN}/trades", params={"from": 0, "to": 9_999_999_999}).json()
    assert body["count"] == 12, "must return the whole range, not a fixed window"
    times = [int(t["time"]) for t in body["trades"]]
    assert times == sorted(times), "trades must be ascending for chart marks"

    mid = c.get(f"/token/{TOKEN}/trades", params={"from": 1000 + 3 * 60, "to": 1000 + 6 * 60}).json()
    assert mid["count"] == 4
    assert all(1000 + 3 * 60 <= int(t["time"]) <= 1000 + 6 * 60 for t in mid["trades"])

    small = c.get(f"/token/{TOKEN}/trades", params={"from": 0, "to": 9_999_999_999, "limit": 5}).json()
    assert small["count"] == 5
    assert small["truncated"] is True

    none = c.get(
        f"/token/{TOKEN}/trades",
        params={"from": 0, "to": 9_999_999_999, "callers": "0x000000000000000000000000000000000000dead"},
    ).json()
    assert none["count"] == 0
    mine = c.get(f"/token/{TOKEN}/trades", params={"from": 0, "to": 9_999_999_999, "callers": USER}).json()
    assert mine["count"] == 12


def test_trades_route_is_not_shadowed_by_the_chartres_route(db):
    """/token/{addr}/{chartres} parses its second segment as an int, so it would
    claim "trades" and 422 if it were registered first."""
    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xz", log_idx=0)

    c = _api_client()
    r = c.get(f"/token/{TOKEN}/trades", params={"from": 0, "to": 9_999_999_999})
    assert r.status_code == 200, f"route shadowed by the chartres route: {r.status_code}"
    assert "trades" in r.json()

    assert c.get(f"/token/{TOKEN}/60").status_code == 200, "numeric resolutions must still work"


def test_stats_exposes_the_reference_price_behind_each_change_pct(db):
    """The client recomputes deltas against a live price, so it needs the exact
    reference rather than inferring it back out of the percentage."""
    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=int(time.time()) - 300, txh="0xs1", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, blk=102, ts=int(time.time()) - 10, txh="0xs2", log_idx=0)

    c = _api_client()
    body = c.get(f"/stats/{TOKEN}").json()

    for w in ("5m", "1h", "6h", "24h"):
        assert f"price_ref_{w}" in body, f"missing price_ref_{w}"
        assert f"change_pct_{w}" in body

    ref = Decimal(body["price_ref_1h"])
    assert ref > 0
    last = Decimal(c.get(f"/token/{TOKEN}/60").json()["marketcap"])
    implied = float((last / ref - 1) * 100)
    assert abs(implied - body["change_pct_1h"]) < 0.01, (implied, body["change_pct_1h"])


def test_realized_pnl_is_zero_until_tokens_are_sold(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xcb1", log_idx=0)

    row = _q(
        db,
        "SELECT realized_pnl_native, cost_basis_native, native_spent FROM launchpad_positions WHERE token=%s",
        (TOKEN,),
    )[0]
    realized, basis, spent = row
    assert realized == 0, f"a position that has only bought has realized nothing, got {realized}"
    assert int(basis) == int(spent) > 0, "the whole spend should sit in the cost basis"


def test_realized_pnl_uses_cost_basis_on_a_partial_sell(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)

    _trade(
        st,
        native_reserve=1100 * 10**18,
        blk=101,
        ts=1001,
        txh="0xcb2",
        log_idx=0,
        is_buy=True,
        amount_in=10**18,
        amount_out=100 * 10**18,
    )
    basis0 = int(_q(db, "SELECT cost_basis_native FROM launchpad_positions WHERE token=%s", (TOKEN,))[0][0])
    assert basis0 == 10**18

    _trade(
        st,
        native_reserve=1050 * 10**18,
        blk=102,
        ts=1002,
        txh="0xcb3",
        log_idx=0,
        is_buy=False,
        amount_in=50 * 10**18,
        amount_out=6 * 10**17,
    )

    realized, basis1, sold = _q(
        db,
        "SELECT realized_pnl_native, cost_basis_native, token_sold FROM launchpad_positions WHERE token=%s",
        (TOKEN,),
    )[0]
    assert int(sold) == 50 * 10**18
    assert int(basis1) == basis0 // 2, f"expected half the basis to remain, got {basis1}"
    proceeds = 1100 * 10**18 - 1050 * 10**18
    assert Decimal(realized) == Decimal(proceeds) - Decimal(basis0 // 2)


def test_closing_a_position_releases_the_entire_basis(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(
        st,
        native_reserve=1100 * 10**18,
        blk=101,
        ts=1001,
        txh="0xcb4",
        log_idx=0,
        is_buy=True,
        amount_in=10**18,
        amount_out=100 * 10**18,
    )
    _trade(
        st,
        native_reserve=1000 * 10**18,
        blk=102,
        ts=1002,
        txh="0xcb5",
        log_idx=0,
        is_buy=False,
        amount_in=100 * 10**18,
        amount_out=9 * 10**17,
    )

    realized, basis = _q(
        db, "SELECT realized_pnl_native, cost_basis_native FROM launchpad_positions WHERE token=%s", (TOKEN,)
    )[0]
    assert int(basis) == 0, "a fully closed position holds no cost basis"
    proceeds = 1100 * 10**18 - 1000 * 10**18
    assert Decimal(realized) == Decimal(proceeds) - Decimal(10**18)


def test_fee_accrual_follows_the_actual_on_chain_rate(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    st.mon_price_usd = Decimal(1)
    st.tokenToPrice[WMON] = Decimal(1)

    from modules import launchpad as lp_mod

    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=1001, txh="0xfe0", log_idx=0)
    lp = st.launchpad_tokens[TOKEN]
    fees_before = lp.fees_usd

    gross_in = 10 * 10**18
    credited = int(gross_in * 95 / 100)
    new_reserve = 1000 * 10**18 + credited
    ev = lp_mod.parse_launchpad_trade(
        _router(),
        ["0x", _ta(TOKEN), _ta(USER)],
        _lt_data(True, gross_in, 10**20, new_reserve, _reserve_for(new_reserve)),
    )
    st.apply_launchpad_trade(ev, 102, 1002, "0xfe1", 0, _router())

    charged = lp.fees_usd - fees_before
    expected = Decimal(gross_in - credited) / Decimal(10**18)
    assert abs(charged - expected) < Decimal("0.0001"), f"expected {expected} got {charged}"
    assert abs(charged - Decimal("0.1")) > Decimal("0.01"), "fee must not be a hardcoded 1%"


def test_nadfun_v2_tokens_backfill_to_source_2(db):
    """v2 tokens indexed before the split carry source 1. The migration moves
    them using the marker table, and must be safe to re-run."""
    import core.storage as storage

    v1_token = "0x1111111111111111111111111111111111111111"
    v2_token = "0x2222222222222222222222222222222222222222"

    for tok in (v1_token, v2_token):
        _x(
            db,
            """
            INSERT INTO launchpad_tokens (token, creator, name, symbol, source, created_block, created_at)
            VALUES (%s, %s, 'n', 's', 1, 1, 1)
            ON CONFLICT (token) DO UPDATE SET source = 1
            """,
            (tok, CREATOR),
        )
    _x(db, "INSERT INTO nadfun_v2_tokens (token) VALUES (%s) ON CONFLICT DO NOTHING", (v2_token,))

    def source_of(tok):
        return int(_q(db, "SELECT source FROM launchpad_tokens WHERE token=%s", (tok,))[0][0])

    assert source_of(v2_token) == 1

    storage.init_db()

    assert source_of(v2_token) == 2, "a v2 token must move to its own source"
    assert source_of(v1_token) == 1, "a v1 token must be left alone"

    storage.init_db()
    assert source_of(v2_token) == 2
    assert source_of(v1_token) == 1

    _x(db, "DELETE FROM nadfun_v2_tokens WHERE token=%s", (v2_token,))
    _x(db, "DELETE FROM launchpad_tokens WHERE token = ANY(%s)", ([v1_token, v2_token],))


def test_holders_endpoint_searches_and_paginates_server_side(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)

    from modules import launchpad as lp_mod

    for i in range(60):
        user = "0x" + f"{i:040x}"
        ev = lp_mod.parse_launchpad_trade(
            _router(),
            ["0x", _ta(TOKEN), _ta(user)],
            _lt_data(True, 10**18, (i + 1) * 10**18, (1100 + i) * 10**18, _reserve_for((1100 + i) * 10**18)),
        )
        st.apply_launchpad_trade(ev, 101 + i, 1001 + i, f"0xh{i}", 0, _router())
        _x(
            db,
            "UPDATE launchpad_positions SET balance_token = %s WHERE user_address = %s AND token = %s",
            ((i + 1) * 10**18, user, TOKEN),
        )

    c = _api_client()

    body = c.get(f"/holders/{TOKEN}", params={"limit": 50}).json()
    assert body["total"] == 60, "total must count every holder, not the page"
    assert body["count"] == 50
    balances = [int(h["balance_token"]) for h in body["holders"]]
    assert balances == sorted(balances, reverse=True), "top holders means largest first"

    page2 = c.get(f"/holders/{TOKEN}", params={"limit": 50, "offset": 50}).json()
    assert page2["count"] == 10

    target = "0x" + f"{3:040x}"
    hit = c.get(f"/holders/{TOKEN}", params={"q": target[-6:]}).json()
    assert hit["total"] >= 1
    assert any(h["account"]["id"] == target for h in hit["holders"])

    miss = c.get(f"/holders/{TOKEN}", params={"q": "zzzzzz"}).json()
    assert miss["total"] == 0 and miss["count"] == 0


def test_advisory_lock_retries_before_giving_up(db):
    import core.storage as storage
    from core.storage import base as sb

    held = storage.acquire_indexer_lock()
    try:
        t0 = time.monotonic()
        try:
            sb.acquire_indexer_lock(wait_seconds=4)
            raise AssertionError("second acquire should not succeed while held")
        except RuntimeError as exc:
            assert "still holds" in str(exc)
        waited = time.monotonic() - t0
        assert waited >= 3.5, f"should have retried for ~4s, only waited {waited:.1f}s"
    finally:
        storage.release_indexer_lock(held)

    again = storage.acquire_indexer_lock(wait_seconds=5)
    storage.release_indexer_lock(again)


def test_cost_basis_backfill_terminates_on_fully_sold_positions(db):
    """A fully sold position has a correct basis of zero, which is the same value
    the predicate selects on -- it was rewritten and re-selected forever, so the
    indexer never got past the backfill and stopped indexing entirely."""
    import core.storage as storage

    rows = [
        ("0xu1", "0xt1", 10**18, 100, 100, True),
        ("0xu2", "0xt2", 10**18, 100, 200, True),
        ("0xu3", "0xt3", 10**18, 100, 0, False),
        ("0xu4", "0xt4", 10**18, 100, 50, False),
    ]
    for user, tok, spent, bought, sold, _ in rows:
        _x(
            db,
            """
            INSERT INTO launchpad_positions
                (user_address, token, native_spent, token_bought, token_sold, cost_basis_native)
            VALUES (%s, %s, %s, %s, %s, 0)
            ON CONFLICT (user_address, token) DO UPDATE SET
                native_spent = EXCLUDED.native_spent,
                token_bought = EXCLUDED.token_bought,
                token_sold = EXCLUDED.token_sold,
                cost_basis_native = 0
            """,
            (user, tok, spent, bought, sold),
        )

    import threading

    done = threading.Event()
    err = []

    def run():
        try:
            storage.backfill_cost_basis()
        except Exception as exc:
            err.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(timeout=30), "backfill did not terminate: it re-selects rows whose basis is legitimately 0"
    assert not err, err

    got = dict(
        ((u, tk), int(b))
        for u, tk, b in _q(
            db,
            "SELECT user_address, token, cost_basis_native FROM launchpad_positions WHERE user_address = ANY(%s)",
            ([r[0] for r in rows],),
        )
    )
    for user, tok, spent, bought, sold, zero in rows:
        if zero:
            assert got[(user, tok)] == 0, f"{user} fully sold, basis must be 0"
        else:
            assert got[(user, tok)] > 0, f"{user} still holds, basis must be positive"

    storage.backfill_cost_basis()

    _x(db, "DELETE FROM launchpad_positions WHERE user_address = ANY(%s)", ([r[0] for r in rows],))


NF_TOKEN = "0x9d11aa55e3cf4f2b8a7c1d6e0f3b2c5a4d8e7777"


def _nadfun_seed(st, source, token=NF_TOKEN):
    """Create a nad.fun token directly: its TokenCreated parser needs a full
    on-chain payload, and these tests are about the trade path."""
    import models

    lp = models.LaunchpadToken(
        token=token,
        creator=CREATOR,
        name="NF",
        symbol="NF",
        metadata_cid="",
        description="",
        social1="",
        social2="",
        social3="",
        social4="",
    )
    lp.source = source
    lp.created_block = 100
    lp.created_at = 1000
    lp.quote_token = WMON
    st.launchpad_tokens[token] = lp

    import core.storage as storage

    storage.upsert_token_created(
        token=token,
        creator=CREATOR,
        name="NF",
        symbol="NF",
        metadata_cid="",
        description="",
        social1="",
        social2="",
        social3="",
        social4="",
        source=source,
        created_block=100,
        created_at=1000,
        last_price_native=0,
        quote_token=WMON,
    )
    return lp


def _nadfun_trade(
    st,
    source,
    token_reserve,
    native_reserve,
    blk,
    ts,
    txh,
    is_buy=True,
    amount_in=10**18,
    amount_out=10**20,
    token=NF_TOKEN,
):
    ev = {
        "token": token,
        "user": USER,
        "is_buy": is_buy,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "native_reserve": native_reserve,
        "token_reserve": token_reserve,
    }
    st.apply_launchpad_trade(ev, blk, ts, txh, 0, _router())
    return ev


def test_nadfun_derived_supply_and_reserves_persist(db):
    """Mocked storage cannot see persistence -- that is exactly what hid the
    curve-reserve bug on native."""
    from core.adapters import nadfun as nf

    st = _new_state()
    _nadfun_seed(st, nf.SOURCE_V2)
    geo = nf.geometry_for(nf.SOURCE_V2)

    tr = geo["virtual_token_0"] - (geo["curve_supply"] // 4)
    _nadfun_trade(st, nf.SOURCE_V2, tr, 90_000 * 10**18, 101, 1001, "0xnf1")

    row = _q(
        db,
        """
        SELECT circulating_supply, curve_token_reserve, curve_native_reserve, source
        FROM launchpad_tokens WHERE token = %s
        """,
        (NF_TOKEN,),
    )[0]
    assert int(row[3]) == nf.SOURCE_V2
    assert int(row[1]) == tr, "nad.fun curve reserves must persist like native's"
    assert int(row[2]) == 90_000 * 10**18
    assert int(row[0]) == (geo["virtual_token_0"] - tr) // 10**18


def test_nadfun_missing_sync_does_not_corrupt_persisted_supply(db):
    """A CurveSync can be missed or lost across a restart. Supply must hold its
    last derived value rather than read as a fully sold curve."""
    from core.adapters import nadfun as nf

    st = _new_state()
    _nadfun_seed(st, nf.SOURCE_V2)
    geo = nf.geometry_for(nf.SOURCE_V2)

    tr = geo["virtual_token_0"] - (geo["curve_supply"] // 3)
    _nadfun_trade(st, nf.SOURCE_V2, tr, 85_000 * 10**18, 101, 1001, "0xnfa")
    good = int(_q(db, "SELECT circulating_supply FROM launchpad_tokens WHERE token=%s", (NF_TOKEN,))[0][0])

    _nadfun_trade(st, nf.SOURCE_V2, 0, 0, 102, 1002, "0xnfb")
    after = int(_q(db, "SELECT circulating_supply FROM launchpad_tokens WHERE token=%s", (NF_TOKEN,))[0][0])
    assert after == good, "a missing sync must not move persisted supply"

    full = geo["virtual_token_0"] - geo["curve_supply"]
    assert after != full // 10**18, "and must never read as a fully sold curve"


def test_detail_endpoint_distinguishes_24h_from_lifetime_volume(db):
    """The detail endpoint serves 24h under volumeNative/volumeUsd while the list
    serves lifetime under native_volume/volume_usd. On REDNIT that was 1.97 MON
    against 1,185,594 MON -- same-looking names, 600,000x apart. The explicit
    keys make which is which unambiguous, and lifetime fees had no key at all."""
    import time

    from core.adapters import nadfun as nf

    st = _new_state()
    _nadfun_seed(st, nf.SOURCE_V2)
    geo = nf.geometry_for(nf.SOURCE_V2)

    old_ts = int(time.time()) - 40 * 3600
    tr = geo["virtual_token_0"] - (geo["curve_supply"] // 6)
    _nadfun_trade(st, nf.SOURCE_V2, tr, 80_000 * 10**18, 101, old_ts, "0xold")

    recent_ts = int(time.time()) - 60
    tr2 = geo["virtual_token_0"] - (geo["curve_supply"] // 5)
    _nadfun_trade(st, nf.SOURCE_V2, tr2, 85_000 * 10**18, 102, recent_ts, "0xnew")

    c = _api_client()
    body = c.get(f"/token/{NF_TOKEN}/60").json()

    for key in ("volumeNative", "volume24hUsd", "volumeLifetimeNative", "volumeLifetimeUsd", "feesLifetimeUsd"):
        assert key in body, f"missing {key}"

    lifetime = int(body["volumeLifetimeNative"])
    day = int(body["volumeNative"])
    assert lifetime > day, "lifetime must include the trade outside the 24h window"

    assert body["volumeUsd"] == body["volume24hUsd"]


def test_post_graduation_native_trades_persist_to_the_token_row(db):
    """The non-batch path wrote the trade row and dropped the token aggregate, so
    post-graduation volume lived only in memory and vanished on restart. cz showed
    20.2020 MON / 6 tx on its row against 40.5661 MON / 15 trades."""
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, blk=101, ts=1001, txh="0xcurve", log_idx=0)

    st.apply_market_created(102, 1002, _market_created_ev(), _router())
    st.apply_migrated(103, 1003, {"token": TOKEN}, _router())

    before_native, before_tx = _q(db, "SELECT native_volume, tx_count FROM launchpad_tokens WHERE token=%s", (TOKEN,))[
        0
    ]

    mi = st.addressToMarket.get(MARKET)
    assert mi is not None, "market must be registered for the graduated path"
    st._record_graduated_launchpad_trade_locked(
        lp_addr=TOKEN,
        mi=mi,
        ev={"is_buy": True, "amount_in": 3 * 10**18, "amount_out": 150 * 10**18, "user": USER},
        blk=104,
        ts=1004,
        txh="0xpostgrad",
        log_idx=0,
        cur=None,
        batch=None,
    )

    after_native, after_tx = _q(db, "SELECT native_volume, tx_count FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0]

    assert int(after_native) == int(before_native) + 3 * 10**18, "post-graduation volume must persist"
    assert int(after_tx) == int(before_tx) + 1, "post-graduation tx count must persist"

    rows_native = int(
        _q(db, "SELECT COALESCE(SUM(native_amount),0) FROM launchpad_trades WHERE token=%s", (TOKEN,))[0][0]
    )
    rows_n = int(_q(db, "SELECT COUNT(*) FROM launchpad_trades WHERE token=%s", (TOKEN,))[0][0])
    assert int(after_native) == rows_native
    assert int(after_tx) == rows_n


def test_graduated_native_trades_accrue_the_market_fee(db):
    """A graduated token keeps earning at the market's taker fee. It previously
    accrued nothing, which is most of a native token's lifetime fees."""
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, blk=101, ts=1001, txh="0xc2", log_idx=0)
    st.apply_market_created(102, 1002, _market_created_ev(), _router())
    st.apply_migrated(103, 1003, {"token": TOKEN}, _router())

    lp = st.launchpad_tokens[TOKEN]
    fees_before = lp.fees_usd
    mi = st.addressToMarket.get(MARKET)
    assert mi is not None

    st._record_graduated_launchpad_trade_locked(
        lp_addr=TOKEN,
        mi=mi,
        ev={"is_buy": True, "amount_in": 5 * 10**18, "amount_out": 250 * 10**18, "user": USER},
        blk=104,
        ts=1004,
        txh="0xfee",
        log_idx=0,
        cur=None,
        batch=None,
    )

    assert lp.fees_usd > fees_before, "a graduated token must keep accruing fees"
    rate = st._pool_fee_rate(mi)
    assert rate > 0
    expected = (Decimal(5 * 10**18) / Decimal(10**18)) * st._quote_price_usd(lp.quote_token) * rate
    assert abs((lp.fees_usd - fees_before) - expected) < Decimal("1e-24")


def test_token_aggregates_reconcile_with_their_trade_rows(db):
    """The invariant that exposed the post-graduation persistence bug: a token's
    stored aggregate must equal the sum of its own trade rows, across the curve
    and past graduation alike. Checked per source so a regression on one path
    cannot hide behind the other."""
    from core.adapters import nadfun as nf

    st = _new_state()
    _create(st)
    _trade(st, native_reserve=1400 * 10**18, blk=101, ts=1001, txh="0xr1", log_idx=0)
    _trade(st, native_reserve=2500 * 10**18, blk=102, ts=1002, txh="0xr2", log_idx=0)
    st.apply_market_created(103, 1003, _market_created_ev(), _router())
    st.apply_migrated(104, 1004, {"token": TOKEN}, _router())
    mi = st.addressToMarket.get(MARKET)
    st._record_graduated_launchpad_trade_locked(
        lp_addr=TOKEN,
        mi=mi,
        ev={"is_buy": True, "amount_in": 2 * 10**18, "amount_out": 90 * 10**18, "user": USER},
        blk=105,
        ts=1005,
        txh="0xr3",
        log_idx=0,
        cur=None,
        batch=None,
    )
    st._record_graduated_launchpad_trade_locked(
        lp_addr=TOKEN,
        mi=mi,
        ev={"is_buy": False, "amount_in": 40 * 10**18, "amount_out": 10**18, "user": USER},
        blk=106,
        ts=1006,
        txh="0xr4",
        log_idx=0,
        cur=None,
        batch=None,
    )

    _nadfun_seed(st, nf.SOURCE_V2)
    geo = nf.geometry_for(nf.SOURCE_V2)
    _nadfun_trade(
        st, nf.SOURCE_V2, geo["virtual_token_0"] - (geo["curve_supply"] // 7), 80_000 * 10**18, 107, 1007, "0xr5"
    )

    for tok, label in ((TOKEN, "native"), (NF_TOKEN, "nad.fun")):
        row_native, row_tx = _q(db, "SELECT native_volume, tx_count FROM launchpad_tokens WHERE token=%s", (tok,))[0]
        sum_native, sum_n = _q(
            db,
            "SELECT COALESCE(SUM(native_amount),0), COUNT(*) FROM launchpad_trades WHERE token=%s",
            (tok,),
        )[0]
        assert int(row_native) == int(sum_native), f"{label}: native_volume {row_native} != trade rows {sum_native}"
        assert int(row_tx) == int(sum_n), f"{label}: tx_count {row_tx} != trade rows {sum_n}"
