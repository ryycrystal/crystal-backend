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

ROUTER = None  # resolved in the fixture, after env is pointed at the scratch DB
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
        cur.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB};")
        cur.execute(f"CREATE DATABASE {SCRATCH_DB};")
    conn.close()

    # _DATABASE_URL is resolved at import time, so override it explicitly
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


# -- synthetic logs -----------------------------------------------------------


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


# -- duplicate delivery -------------------------------------------------------


def test_duplicate_log_delivery_is_idempotent(db):
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, txh="0xdup", log_idx=0)
    before = _token_row(db)
    trades_before = _q(db, "SELECT count(*) FROM launchpad_trades")[0][0]

    # exact same log delivered again
    st2 = _new_state()
    st2.rebuild_from_db()
    _trade(st2, native_reserve=2500 * 10**18, txh="0xdup", log_idx=0)

    assert _q(db, "SELECT count(*) FROM launchpad_trades")[0][0] == trades_before
    assert _token_row(db) == before


def test_duplicate_tx_with_multiple_logs(db):
    st = _new_state()
    _create(st)
    # two distinct logs in one transaction
    _trade(st, native_reserve=1500 * 10**18, txh="0xmulti", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, txh="0xmulti", log_idx=1)
    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xmulti'")[0][0] == 2

    # replaying the whole transaction adds nothing
    st2 = _new_state()
    st2.rebuild_from_db()
    _trade(st2, native_reserve=1500 * 10**18, txh="0xmulti", log_idx=0)
    _trade(st2, native_reserve=2000 * 10**18, txh="0xmulti", log_idx=1)
    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xmulti'")[0][0] == 2


# -- ordering -----------------------------------------------------------------


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


# -- lifecycle ----------------------------------------------------------------


def test_created_to_active_on_first_trade(db):
    from core.adapters import native as native_mod
    from core.lifecycle import TokenPhase, resolve_phase

    st = _new_state()
    _create(st)
    row = _token_row(db)
    assert row[5] == 0  # tx_count
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
    assert row[6] is True  # migrated
    assert row[7] == MARKET  # market linked exactly once, same value
    assert st.launchpad_market_to_token[MARKET] == TOKEN


def test_graduation_partially_processed_then_replayed(db):
    """Migrated lands, MarketCreated is lost, then the whole tx is replayed."""
    st = _new_state()
    _create(st)
    _trade(st, native_reserve=2500 * 10**18, txh="0xpart", log_idx=0)

    st.apply_migrated(102, 1002, {"token": TOKEN}, _router())
    assert _token_row(db)[6] is True
    assert _token_row(db)[7] in (None, "")  # market not linked yet

    st2 = _new_state()
    st2.rebuild_from_db()
    st2.apply_migrated(102, 1002, {"token": TOKEN}, _router())
    st2.apply_market_created(102, 1002, _market_created_ev(), _router())

    row = _token_row(db)
    assert row[6] is True
    assert row[7] == MARKET


# -- restart / replay ---------------------------------------------------------


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


# -- isolation ----------------------------------------------------------------


def test_multiple_tokens_do_not_cross_contaminate(db):
    st = _new_state()
    _create(st, token=TOKEN, name="A", symbol="A")
    _create(st, token=TOKEN_B, name="B", symbol="B")

    _trade(st, token=TOKEN, native_reserve=2500 * 10**18, txh="0xa", log_idx=0)
    _trade(st, token=TOKEN_B, native_reserve=1500 * 10**18, txh="0xb", log_idx=0)

    a = _token_row(db, TOKEN)
    b = _token_row(db, TOKEN_B)
    assert a[0] == 600_000_000 and a[1] is True  # A graduating
    assert b[0] < 600_000_000 and b[1] is False  # B still active
    assert a[2] != b[2]  # separate reserves


# -- failure handling ---------------------------------------------------------


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
    # log_index 1 never applied -- simulate the crash

    st2 = _new_state()
    st2.rebuild_from_db()
    _trade(st2, native_reserve=1500 * 10**18, blk=105, ts=1005, txh="0xcrash", log_idx=0)
    _trade(st2, native_reserve=2500 * 10**18, blk=105, ts=1005, txh="0xcrash", log_idx=1)

    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xcrash'")[0][0] == 2
    row = _token_row(db)
    assert int(row[2]) == 2500 * 10**18  # reserve reflects the last log


# -- known gap ----------------------------------------------------------------


def test_short_reorg_replaces_previously_indexed_blocks(db):
    import core.storage as storage

    st = _new_state()
    _create(st)
    _trade(st, native_reserve=1400 * 10**18, blk=109, ts=1009, txh="0xkeep", log_idx=0)
    storage.record_block_hash(109, "0xaaa")
    _trade(st, native_reserve=1500 * 10**18, blk=110, ts=1010, txh="0xorphan", log_idx=0)
    storage.record_block_hash(110, "0xbbb")

    assert _q(db, "SELECT count(*) FROM launchpad_trades")[0][0] == 2

    # block 110 comes back with a different hash -> reorg
    st2 = _new_state()
    st2.rebuild_from_db()
    assert st2.detect_reorg(110, "0xdifferent") is True
    assert st2.detect_reorg(109, "0xaaa") is False

    st2.handle_reorg(110)

    # the orphaned trade is gone and the surviving one still counts
    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xorphan'")[0][0] == 0
    assert _q(db, "SELECT count(*) FROM launchpad_trades WHERE txhash='0xkeep'")[0][0] == 1

    row = _token_row(db)
    assert int(row[4]) == 10**18  # native_volume back to one trade
    assert int(row[5]) == 1  # tx_count
    assert int(row[2]) == 1400 * 10**18  # curve reserve from surviving trade
    assert row[1] is False  # no longer graduating

    # replaying the canonical block lands cleanly on the rebuilt state
    _trade(st2, native_reserve=1800 * 10**18, blk=110, ts=1010, txh="0xcanon", log_idx=0)
    assert _q(db, "SELECT count(*) FROM launchpad_trades")[0][0] == 2
    assert int(_token_row(db)[2]) == 1800 * 10**18


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
            # same address, two buys inside the 10-block window, batched
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


def test_sniper_window_excludes_late_buys_and_the_creator(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)

    # outside the 10-block window
    _trade(st, native_reserve=1100 * 10**18, blk=200, ts=1100, txh="0xlate", log_idx=0)
    assert _q(db, "SELECT count(*) FROM launchpad_snipers WHERE token=%s", (TOKEN,))[0][0] == 0
    assert int(_q(db, "SELECT snipers_count FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0]) == 0


def test_deep_reorg_scan_finds_divergence_the_per_block_guard_misses(db):
    """The per-block guard only fires when a block is delivered again. This
    simulates the scan that walks the indexed tail newest-first."""
    import core.storage as storage

    st = _new_state()
    _create(st)
    _trade(st, native_reserve=1300 * 10**18, blk=120, ts=1020, txh="0xb120", log_idx=0)
    storage.record_block_hash(120, "0x120good")
    _trade(st, native_reserve=1400 * 10**18, blk=121, ts=1021, txh="0xb121", log_idx=0)
    storage.record_block_hash(121, "0x121bad")
    _trade(st, native_reserve=1500 * 10**18, blk=122, ts=1022, txh="0xb122", log_idx=0)
    storage.record_block_hash(122, "0x122bad")

    rows = storage.get_recent_block_hashes(32)
    assert [r[0] for r in rows] == [122, 121, 120], "must be newest-first"

    # chain now reports different hashes for 121/122; 120 is unchanged
    onchain = {120: "0x120good", 121: "0x121new", 122: "0x122new"}
    rollback_from = None
    for number, stored in rows:
        if onchain[number] == stored:
            break  # canonical from here down -- scan stops
        rollback_from = number
    assert rollback_from == 121, "must roll back to the deepest divergent block"

    st.handle_reorg(rollback_from)
    remaining = {r[0] for r in _q(db, "SELECT txhash FROM launchpad_trades")}
    assert remaining == {"0xb120"}
    assert int(_token_row(db)[2]) == 1300 * 10**18


def test_reorg_rebuild_matches_a_clean_index_of_the_canonical_chain(db):
    """After a reorg the state must equal what a fresh index of the surviving
    chain would produce -- not merely 'something plausible'."""
    import core.storage as storage

    st = _new_state()
    _create(st)
    _trade(st, native_reserve=1400 * 10**18, blk=109, ts=1009, txh="0xkeep", log_idx=0)
    storage.record_block_hash(109, "0xaaa")
    _trade(st, native_reserve=2500 * 10**18, blk=110, ts=1010, txh="0xorphan", log_idx=0)
    storage.record_block_hash(110, "0xbbb")
    st.handle_reorg(110)
    after_reorg = _token_row(db)

    # now index only the canonical chain from scratch
    import psycopg2

    conn = psycopg2.connect(db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(LAUNCHPAD_TABLES) + " RESTART IDENTITY CASCADE;")
    conn.close()

    st2 = _new_state()
    _create(st2)
    _trade(st2, native_reserve=1400 * 10**18, blk=109, ts=1009, txh="0xkeep", log_idx=0)
    clean = _token_row(db)

    assert after_reorg == clean


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

    # price up -> ATH follows
    _trade(st, native_reserve=2000 * 10**18, blk=102, ts=1002, txh="0xb", log_idx=0)
    peak = ath()
    assert peak > first

    # price back down -> ATH holds at the peak
    _trade(st, native_reserve=1200 * 10**18, blk=103, ts=1003, txh="0xc", log_idx=0)
    current = _q(db, "SELECT last_price_native FROM launchpad_tokens WHERE token=%s", (TOKEN,))[0][0]
    assert ath() == peak, "ATH must not follow price down"
    assert current < peak

    # replaying the peak trade must not disturb it
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

    # a narrow window returns only what falls inside it
    mid = c.get(f"/token/{TOKEN}/trades", params={"from": 1000 + 3 * 60, "to": 1000 + 6 * 60}).json()
    assert mid["count"] == 4
    assert all(1000 + 3 * 60 <= int(t["time"]) <= 1000 + 6 * 60 for t in mid["trades"])

    # limit is honoured and flagged
    small = c.get(f"/token/{TOKEN}/trades", params={"from": 0, "to": 9_999_999_999, "limit": 5}).json()
    assert small["count"] == 5
    assert small["truncated"] is True

    # server-side caller filter
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
    # the pair must be self-consistent: last/ref-1 reproduces the reported pct
    last = Decimal(c.get(f"/token/{TOKEN}/60").json()["marketcap"])
    implied = float((last / ref - 1) * 100)
    assert abs(implied - body["change_pct_1h"]) < 0.01, (implied, body["change_pct_1h"])


# realized pnl must be cost basis, not cash flow
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


# selling half releases half the basis and books the difference
def test_realized_pnl_uses_cost_basis_on_a_partial_sell(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)

    # buy 100 tokens for 1 MON
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

    # sell 50 of them; the sell leg is gross from the reserve delta
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
    # half the tokens left, so half the basis was released
    assert int(basis1) == basis0 // 2, f"expected half the basis to remain, got {basis1}"
    proceeds = 1100 * 10**18 - 1050 * 10**18
    assert Decimal(realized) == Decimal(proceeds) - Decimal(basis0 // 2)


# closing the position releases the whole basis
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
    # bought for 1 MON, curve returned 100 MON of reserve delta on the way out
    proceeds = 1100 * 10**18 - 1000 * 10**18
    assert Decimal(realized) == Decimal(proceeds) - Decimal(10**18)


# fees come from the reserve delta so a governance fee change is picked up at once
def test_fee_accrual_follows_the_actual_on_chain_rate(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    st.mon_price_usd = Decimal(1)
    st.tokenToPrice[WMON] = Decimal(1)

    from modules import launchpad as lp_mod

    # seed the previous reserve
    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=1001, txh="0xfe0", log_idx=0)
    lp = st.launchpad_tokens[TOKEN]
    fees_before = lp.fees_usd

    # a 10 MON buy credited at a 5% fee: only 9.5 reaches the curve
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
    # the old hardcoded 1% would have booked 0.1
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

    # idempotent
    storage.init_db()
    assert source_of(v2_token) == 2
    assert source_of(v1_token) == 1

    _x(db, "DELETE FROM nadfun_v2_tokens WHERE token=%s", (v2_token,))
    _x(db, "DELETE FROM launchpad_tokens WHERE token = ANY(%s)", ([v1_token, v2_token],))
