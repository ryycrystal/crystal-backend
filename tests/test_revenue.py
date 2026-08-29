import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.storage.launchpad import record_revenue_sample  # noqa: E402

E = 10**18


class FakeCur:
    def __init__(self, prev):
        self.prev = prev
        self.inserted = None

    def execute(self, sql, params=None):
        if "INSERT" in sql:
            self.inserted = params

    def fetchone(self):
        return self.prev


def test_first_sample_books_no_revenue():
    cur = FakeCur(None)
    out = record_revenue_sample(100, 1700, 95 * E, "0.03", cur=cur)
    assert out["first"] is True
    assert out["delta_wei"] == 0


def test_delta_is_the_balance_increase():
    cur = FakeCur((90, 95 * E))
    out = record_revenue_sample(100, 1700, 97 * E, "0.03", cur=cur)
    assert out["delta_wei"] == 2 * E
    assert out["delta_usd"] == Decimal("0.06")


def test_a_withdrawal_never_books_negative_revenue():
    cur = FakeCur((90, 95 * E))
    out = record_revenue_sample(100, 1700, 10 * E, "0.03", cur=cur)
    assert out["delta_wei"] == 0
    assert out["delta_usd"] == Decimal(0)


def test_stale_or_repeated_block_is_ignored():
    assert record_revenue_sample(90, 1700, 99 * E, "0.03", cur=FakeCur((90, 95 * E))) is None
    assert record_revenue_sample(89, 1700, 99 * E, "0.03", cur=FakeCur((90, 95 * E))) is None


def test_missing_price_still_records_native():
    cur = FakeCur((90, 95 * E))
    out = record_revenue_sample(100, 1700, 96 * E, None, cur=cur)
    assert out["delta_wei"] == E
    assert out["delta_usd"] == Decimal(0)


def test_row_written_carries_block_and_delta():
    cur = FakeCur((90, 95 * E))
    record_revenue_sample(101, 1701, 96 * E, "0.03", cur=cur)
    assert cur.inserted[0] == 101
    assert cur.inserted[1] == 1701
    assert cur.inserted[2] == 96 * E
    assert cur.inserted[3] == E
