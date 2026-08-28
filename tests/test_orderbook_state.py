"""orderbook applier semantics against a real database.

the invariants: an event row's primary key is the replay guard, so re-applying a
block can never double-count a decrease; adds, removals, decreases and fills
evolve the order row the way the book evolved on chain.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import core.storage as storage  # noqa: E402
from tests.test_launchpad_integration import (  # noqa: E402
    clean,  # noqa: F401
    db,  # noqa: F401
)

MARKET = "0xc8045b5dde24e625932df738e7ec4127c04008d3"
USER = "0x581172970bda012d71a9aea34a9f219da117891b"


@pytest.fixture(autouse=True)
def _clean_orderbook(db):
    with storage.db_cursor() as cur:
        for t in (
            "crystal_orderbook_events",
            "crystal_orderbook_orders",
            "crystal_orderbook_fills",
            "crystal_market_trades",
        ):
            cur.execute(f"DELETE FROM {t}")
    yield


def _entry(flag, price, oid, size):
    from modules.orderbook import ACTIONS

    return {
        "flag": flag,
        "action": ACTIONS[flag],
        "is_buy": flag % 2 == 0,
        "price": price,
        "order_id": oid,
        "size": size,
    }


def _apply(orders, txh, li=0, blk=100, ts=1000):
    storage.apply_orderbook_updates({"market": MARKET, "user": USER, "orders": orders}, blk, ts, txh, li)


def test_order_lifecycle(db):
    _apply([_entry(2, 500, 7, 12345)], "0xob1", blk=100, ts=1000)
    open_orders = storage.list_open_orders(USER)
    assert len(open_orders) == 1
    assert open_orders[0]["order_id"] == 7 and open_orders[0]["size"] == "12345"
    assert open_orders[0]["is_buy"] is True

    _apply([_entry(4, 500, 7, 345)], "0xob2", blk=101, ts=1001)
    open_orders = storage.list_open_orders(USER)
    assert open_orders[0]["size"] == "12000", "a decrease entry carries the decrement"

    _apply([_entry(0, 500, 7, 12000)], "0xob3", blk=102, ts=1002)
    assert storage.list_open_orders(USER) == [], "a removal closes the order"

    events = storage.list_orderbook_events(USER)
    assert [e["action"] for e in events] == ["remove", "decrease", "add"]


def test_replay_is_idempotent(db):
    _apply([_entry(2, 500, 9, 1000)], "0xob4", blk=100, ts=1000)
    _apply([_entry(4, 500, 9, 100)], "0xob5", blk=101, ts=1001)
    _apply([_entry(4, 500, 9, 100)], "0xob5", blk=101, ts=1001)

    open_orders = storage.list_open_orders(USER)
    assert open_orders[0]["size"] == "900", "the replayed decrease must be a no-op"


def test_fill_updates_order_remaining(db):
    _apply([_entry(3, 700, 11, 5000)], "0xob6", blk=100, ts=1000)

    fill = {
        "market": MARKET,
        "maker": USER,
        "flag": 1,
        "maker_is_buy": True,
        "price": 700,
        "order_id": 11,
        "remaining": 2000,
        "amount_high": 3000,
        "amount_out": 2990,
    }
    storage.apply_orderbook_fill(fill, 101, 1001, "0xfill1", 0)
    open_orders = storage.list_open_orders(USER)
    assert open_orders[0]["size"] == "2000", "the fill leaves the logged remaining size"

    fill2 = {**fill, "remaining": 0}
    storage.apply_orderbook_fill(fill2, 102, 1002, "0xfill2", 0)
    assert storage.list_open_orders(USER) == [], "a zero remaining closes the order"

    storage.apply_orderbook_fill(fill2, 102, 1002, "0xfill2", 0)
    with storage.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM crystal_orderbook_fills")
        assert cur.fetchone()[0] == 2, "a replayed fill inserts nothing"


def test_out_of_order_replay_never_regresses_state(db):
    _apply([_entry(2, 900, 5, 4000)], "0xlive1", blk=200, ts=2000)

    _apply([_entry(2, 500, 5, 12345)], "0xold1", blk=100, ts=1000)
    open_orders = storage.list_open_orders(USER)
    assert open_orders[0]["price"] == "900" and open_orders[0]["size"] == "4000", (
        "a stale add must not overwrite the live order state"
    )

    _apply([_entry(0, 500, 5, 12345)], "0xold2", blk=101, ts=1001)
    assert storage.list_open_orders(USER)[0]["size"] == "4000", "a stale remove must not close the live order"

    _apply([_entry(4, 500, 5, 100)], "0xold3", blk=102, ts=1002)
    assert storage.list_open_orders(USER)[0]["size"] == "4000", "a stale decrease must not shrink the live order"

    old_fill = {
        "market": MARKET,
        "maker": USER,
        "flag": 1,
        "maker_is_buy": True,
        "price": 500,
        "order_id": 5,
        "remaining": 7,
        "amount_high": 10,
        "amount_out": 9,
    }
    storage.apply_orderbook_fill(old_fill, 103, 1003, "0xoldfill", 0)
    assert storage.list_open_orders(USER)[0]["size"] == "4000", "a stale fill must not resize the live order"

    assert len(storage.list_orderbook_events(USER)) == 4, "history still records every replayed event"
    with storage.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM crystal_orderbook_fills")
        assert cur.fetchone()[0] == 1, "the stale fill still lands in the fills table"


def test_batch_inserts_report_fresh_rows_once(db):
    ev_row = ("0xbatch1", 0, 0, 100, 1000, MARKET, USER, 2, True, "add", 500, 21, 7777)
    fill_row = ("0xbatch2", 1, 101, 1001, MARKET, USER, True, 500, 21, 0, 10, 9)

    with storage.db_cursor() as cur:
        assert storage.batch_insert_orderbook_events([ev_row], cur) == {("0xbatch1", 0, 0)}
        assert storage.batch_insert_orderbook_events([ev_row], cur) == set(), "a replayed row is not fresh"
        assert storage.batch_insert_orderbook_fills([fill_row], cur) == {("0xbatch2", 1)}
        assert storage.batch_insert_orderbook_fills([fill_row], cur) == set()

    _apply([_entry(2, 500, 21, 7777)], "0xbatch3", blk=200, ts=2000)
    with storage.db_cursor() as cur:
        latest = storage.get_order_updated_blocks([(MARKET, 500, 21), (MARKET, 500, 99)], cur)
    assert latest == {(MARKET, 500, 21): 200}, "only rows that exist come back, at their live block"


def test_decrease_shrinks_the_order_not_just_the_remainder(db):
    _apply([_entry(2, 500, 61, 1000)], "0xdec1", blk=100, ts=1000)
    _apply([_entry(4, 500, 61, 300)], "0xdec2", blk=101, ts=1001)

    row = next(r for r in storage.list_open_orders(USER) if r["order_id"] == 61)
    assert row["size"] == "700", "the resting size drops by the decreased amount"
    assert row["original_size"] == "700", "the order is now a 700 order, not a 1000 order"
    assert row["filled_size"] == "0", "a decrease executes nothing"

    _apply([_entry(3, 900, 62, 1000)], "0xdec3", blk=102, ts=1002)
    storage.apply_orderbook_fill(
        {
            "market": MARKET,
            "maker": USER,
            "flag": 1,
            "maker_is_buy": True,
            "price": 900,
            "order_id": 62,
            "remaining": 400,
            "amount_high": 600,
            "amount_out": 599,
        },
        103,
        1003,
        "0xdec4",
        0,
    )
    _apply([_entry(5, 900, 62, 900)], "0xdec5", blk=104, ts=1004)
    row = next(r for r in storage.list_wallet_orders(USER) if r["order_id"] == 62)
    assert row["filled_size"] == "600"
    assert int(row["original_size"]) >= int(row["filled_size"]), "filled can never exceed the original"


def test_canceled_order_is_not_reported_as_filled(db):
    _apply([_entry(2, 500, 41, 1000)], "0xcan1", blk=100, ts=1000)
    _apply([_entry(0, 500, 41, 1000)], "0xcan2", blk=101, ts=1001)

    row = next(r for r in storage.list_wallet_orders(USER) if r["order_id"] == 41)
    assert row["status"] == "canceled"
    assert row["filled_size"] == "0", "a cancelled order executed nothing"
    assert row["original_size"] == "1000"

    _apply([_entry(3, 700, 42, 1000)], "0xcan3", blk=102, ts=1002)
    fill = {
        "market": MARKET,
        "maker": USER,
        "flag": 1,
        "maker_is_buy": True,
        "price": 700,
        "order_id": 42,
        "remaining": 600,
        "amount_high": 400,
        "amount_out": 399,
    }
    storage.apply_orderbook_fill(fill, 103, 1003, "0xcan4", 0)
    _apply([_entry(1, 700, 42, 600)], "0xcan5", blk=104, ts=1004)

    row = next(r for r in storage.list_wallet_orders(USER) if r["order_id"] == 42)
    assert row["status"] == "canceled" and row["filled_size"] == "400", (
        "a partial fill then cancel reports only the executed amount"
    )

    _apply([_entry(3, 900, 43, 1000)], "0xcan6", blk=105, ts=1005)
    storage.apply_orderbook_fill({**fill, "price": 900, "order_id": 43, "remaining": 0}, 106, 1006, "0xcan7", 0)
    row = next(r for r in storage.list_wallet_orders(USER) if r["order_id"] == 43)
    assert row["status"] == "filled" and row["filled_size"] == "1000"


def test_same_timestamp_rows_have_a_stable_order(db):
    for price in (500, 900, 700, 300, 1100):
        _apply([_entry(2, price, 1, 1000)], f"0xtie{price}", li=price, blk=100, ts=1000)
        _trade(f"0xtietr{price}", 1000, li=price)

    def snapshot():
        return (
            [(r["price"], r["order_id"]) for r in storage.list_open_orders(USER)],
            [(r["price"], r["order_id"]) for r in storage.list_wallet_orders(USER)],
            [r["txhash"] for r in storage.list_exchange_trades(USER)],
            [(r["txhash"], r["log_index"]) for r in storage.list_order_history(USER)],
        )

    first = snapshot()
    assert first[0] == [("1100", 1), ("900", 1), ("700", 1), ("500", 1), ("300", 1)], (
        "tied rows fall back to price, so a ladder reads in price order"
    )
    for _ in range(5):
        assert snapshot() == first, "repeated reads must not reshuffle tied rows"


def test_same_native_id_at_two_price_levels(db):
    _apply([_entry(2, 500, 1, 1000)], "0xlvl1", blk=100, ts=1000)
    _apply([_entry(3, 700, 1, 2000)], "0xlvl2", blk=101, ts=1001)

    open_orders = storage.list_open_orders(USER)
    assert len(open_orders) == 2, "one id at two price levels is two orders"

    _apply([_entry(0, 500, 1, 1000)], "0xlvl3", blk=102, ts=1002)
    open_orders = storage.list_open_orders(USER)
    assert len(open_orders) == 1 and open_orders[0]["price"] == "700", (
        "removing the level-500 order leaves the level-700 order untouched"
    )


def _trade(txh, ts, is_buy=True, market=MARKET, li=0):
    parsed = {
        "market": market,
        "user": USER,
        "is_buy": is_buy,
        "amount_in": 1000,
        "amount_out": 990,
        "start_price": 500,
        "end_price": 505,
    }
    storage.insert_market_trade(parsed, 100, ts, txh, li)


def test_exchange_trades_merges_taker_and_maker(db):
    _trade("0xtr1", 1000)
    _trade("0xtr2", 3000, is_buy=False)
    fill = {
        "market": MARKET,
        "maker": USER,
        "flag": 1,
        "maker_is_buy": True,
        "price": 700,
        "order_id": 11,
        "remaining": 0,
        "amount_high": 3000,
        "amount_out": 2990,
    }
    storage.apply_orderbook_fill(fill, 101, 2000, "0xfillx", 0)

    rows = storage.list_exchange_trades(USER)
    assert [r["kind"] for r in rows] == ["taker", "maker", "taker"], "newest first across both sources"
    assert [r["timestamp"] for r in rows] == [3000, 2000, 1000]
    assert rows[1]["order_id"] == 11 and rows[0]["order_id"] is None

    _trade("0xtr1", 1000)
    assert len(storage.list_exchange_trades(USER)) == 3, "a replayed trade inserts nothing"

    page = storage.list_exchange_trades(USER, before_ts=3000)
    assert [r["timestamp"] for r in page] == [2000, 1000], "before_ts pages strictly backwards"

    only = storage.list_exchange_trades(USER, market="0x" + "9" * 40)
    assert only == [], "a market filter excludes other markets"


def test_batch_tx_trades_merge_into_one_row(db):
    _trade("0xbatchtx", 5000, li=3)
    _trade("0xbatchtx", 5000, li=7)

    rows = storage.list_exchange_trades(USER, before_ts=5001)
    assert len(rows) == 1, "two trade legs in one tx serve as one row"
    assert rows[0]["amount_in"] == "2000" and rows[0]["amount_out"] == "1980", "amounts are exact sums"
    assert rows[0]["legs"] == 2

    fill = {
        "market": MARKET,
        "maker": USER,
        "flag": 1,
        "maker_is_buy": True,
        "price": 700,
        "order_id": 31,
        "remaining": 5,
        "amount_high": 3000,
        "amount_out": 2990,
    }
    storage.apply_orderbook_fill(fill, 110, 6000, "0xsweeptx", 2)
    storage.apply_orderbook_fill({**fill, "order_id": 32, "remaining": 0}, 110, 6000, "0xsweeptx", 5)
    rows = storage.list_exchange_trades(USER, before_ts=6001)
    top = rows[0]
    assert top["kind"] == "maker" and top["legs"] == 2, "two fills in one sweep serve as one row"
    assert top["amount_in"] == "5980" and top["amount_out"] == "6000", "maker view: in is what the taker paid out"
    assert top["order_id"] is None, "a merged row spans orders, so no single order id"


def test_order_history_includes_fills(db):
    _apply([_entry(3, 700, 11, 5000)], "0xob7", blk=100, ts=1000)
    fill = {
        "market": MARKET,
        "maker": USER,
        "flag": 1,
        "maker_is_buy": True,
        "price": 700,
        "order_id": 11,
        "remaining": 2000,
        "amount_high": 3000,
        "amount_out": 2990,
    }
    storage.apply_orderbook_fill(fill, 101, 1001, "0xfilly", 0)

    rows = storage.list_order_history(USER)
    assert [r["action"] for r in rows] == ["fill", "add"]
    assert rows[0]["size"] == "2990", "a fill row carries the executed amount"


def test_stale_indexer_refuses_orderbook_reads(db, monkeypatch):
    import pytest as _pytest
    from fastapi import HTTPException

    from api.routes import orderbook as ob_routes

    monkeypatch.setattr(ob_routes, "STALE_SECONDS", 300.0)
    with _pytest.raises(HTTPException) as exc:
        ob_routes.open_orders(USER)
    assert exc.value.status_code == 503, "history-era data serves 503, not an empty 200"

    monkeypatch.setattr(ob_routes, "STALE_SECONDS", 0.0)
    assert ob_routes.open_orders(USER)["orders"] == [], "a disabled gate serves normally"


def test_orderbook_routes_serve_the_readers(db):
    from api.routes import orderbook as ob_routes

    _apply([_entry(2, 500, 7, 12345)], "0xob8", blk=100, ts=1000)
    _trade("0xtr3", 2000)

    body = ob_routes.open_orders(USER)
    assert body["count"] == 1 and body["orders"][0]["order_id"] == 7

    body = ob_routes.exchange_trades(USER)
    assert body["count"] == 1 and body["trades"][0]["kind"] == "taker"
    assert body["next_before_ts"] is None, "a short page reports no further pages"

    body = ob_routes.order_history(USER)
    assert body["count"] == 1, "taker trades live in the trades plane, not order history"

    import pytest as _pytest
    from fastapi import HTTPException

    with _pytest.raises(HTTPException):
        ob_routes.open_orders("not-an-address")


def test_reads_span_several_wallets(db):
    other = "0x082345baa3bdf6029f27603ddeac103db0114c85"
    _apply([_entry(2, 500, 71, 1000)], "0xmw1", blk=100, ts=1000)
    storage.apply_orderbook_updates(
        {"market": MARKET, "user": other, "orders": [_entry(3, 900, 72, 2000)]}, 101, 1001, "0xmw2", 0
    )

    assert len(storage.list_open_orders(USER)) == 1, "one wallet sees only its own"
    assert len(storage.list_open_orders(other)) == 1
    both = storage.list_open_orders([USER, other])
    assert len(both) == 2, "the selected set sees the union"
    assert {r["order_id"] for r in both} == {71, 72}

    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO crystal_markets
                (market, is_canonical, quote_asset, base_asset, quote_address, quote_decimals,
                 quote_ticker, quote_name, base_address, base_decimals, base_ticker, base_name)
            VALUES (%s, TRUE, 'USDC', 'WMON', %s, 6, 'USDC', 'USD Coin', %s, 18, 'WMON', 'Wrapped MON')
            ON CONFLICT (market) DO NOTHING
            """,
            (MARKET, "0x" + "1" * 40, "0x" + "2" * 40),
        )

    one = storage.open_order_locked_by_token(USER)
    union = storage.open_order_locked_by_token([USER, other])
    assert sum(union.values()) > sum(one.values()), "the union locks more than one wallet alone"
    assert set(union) == {"0x" + "1" * 40, "0x" + "2" * 40}

    assert len(storage.list_wallet_orders([USER, other])) == 2
    assert len(storage.list_order_history([USER, other])) == 2


def test_wallet_prefs_store_indices_under_an_opaque_key(db):
    import pytest as _pytest
    from fastapi import HTTPException

    from api.routes import orderbook as ob_routes

    key = "a" * 64
    assert ob_routes.read_wallet_prefs(key) == {"count": 0, "selected": [], "updatedAt": 0}
    ob_routes.write_wallet_prefs(key, {"count": 5, "selected": [0, 2, 2, 3]})
    got = ob_routes.read_wallet_prefs(key)
    assert got["count"] == 5 and got["selected"] == [0, 2, 3], "duplicates collapse, order is stable"

    with _pytest.raises(HTTPException):
        ob_routes.read_wallet_prefs("0x25afd36012fa25336cc56a1b26c56e92dd77f0f3")
    with _pytest.raises(HTTPException):
        ob_routes.write_wallet_prefs(key, {"count": 99, "selected": []})
