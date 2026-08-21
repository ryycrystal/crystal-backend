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


# the shared clean fixture only covers the launchpad tables, so the orderbook
# plane resets here to keep each test's book independent
@pytest.fixture(autouse=True)
def _clean_orderbook(db):
    with storage.db_cursor() as cur:
        for t in ("crystal_orderbook_events", "crystal_orderbook_orders", "crystal_orderbook_fills"):
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


# add then decrease then remove walks the order through its lifecycle
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


# replaying the same log must not double-apply the decrease
def test_replay_is_idempotent(db):
    _apply([_entry(2, 500, 9, 1000)], "0xob4", blk=100, ts=1000)
    _apply([_entry(4, 500, 9, 100)], "0xob5", blk=101, ts=1001)
    _apply([_entry(4, 500, 9, 100)], "0xob5", blk=101, ts=1001)

    open_orders = storage.list_open_orders(USER)
    assert open_orders[0]["size"] == "900", "the replayed decrease must be a no-op"


# a fill records the maker row and moves the order to its remaining size
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
