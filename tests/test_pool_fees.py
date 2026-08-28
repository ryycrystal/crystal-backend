import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod  # noqa: E402
from core.storage.launchpad import get_pool_fee_rate  # noqa: E402


class FakeCur:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.row


def test_native_taker_fee_is_the_remainder_out_of_100000():
    for taker_fee, expected in ((99910, "0.0009"), (99970, "0.0003"), (99990, "0.0001")):
        cur = FakeCur((taker_fee, 0, True))
        rate = get_pool_fee_rate("0xMarket", 0, cur=cur)
        assert rate == Decimal(expected)
        assert "crystal_markets" in cur.sql


def test_v2_collector_rate_is_bps_out_of_10000():
    cur = FakeCur((135, 0, True))
    rate = get_pool_fee_rate("0xPair", 2, cur=cur)
    assert rate == Decimal("0.0135")
    assert "launchpad_pair_fees" in cur.sql


def test_v1_pools_fall_back_to_the_v3_fee_tier():
    rate = get_pool_fee_rate("0xPair", 1, cur=FakeCur((0, 10000, False)))
    assert rate == Decimal("0.01")


def test_v3_tier_is_ignored_when_a_collector_rate_exists():
    rate = get_pool_fee_rate("0xPair", 2, cur=FakeCur((135, 10000, True)))
    assert rate == Decimal("0.0135")


def test_other_v3_tiers_convert_from_ppm():
    assert get_pool_fee_rate("0xPair", 1, cur=FakeCur((0, 3000, False))) == Decimal("0.003")
    assert get_pool_fee_rate("0xPair", 1, cur=FakeCur((0, 500, False))) == Decimal("0.0005")


def test_market_is_lowercased_before_lookup():
    cur = FakeCur((135, 0, True))
    get_pool_fee_rate("0xAbCdEf", 2, cur=cur)
    assert cur.params == ("0xabcdef",)


def test_no_row_means_no_rate():
    assert get_pool_fee_rate("0xPair", 1, cur=FakeCur(None)) is None
    assert get_pool_fee_rate("0xMarket", 0, cur=FakeCur((None, 0, True))) is None


def test_pair_with_neither_rate_is_none():
    assert get_pool_fee_rate("0xPair", 1, cur=FakeCur((0, 0, False))) is None


def test_blank_market_never_queries():
    cur = FakeCur((135, 0, True))
    assert get_pool_fee_rate("", 1, cur=cur) is None
    assert cur.sql == ""


def test_absurd_rates_are_rejected():
    assert get_pool_fee_rate("0xPair", 2, cur=FakeCur((900, 0, True))) is None
    assert get_pool_fee_rate("0xMarket", 0, cur=FakeCur((100000, 0, True))) is None
    assert get_pool_fee_rate("0xPair", 1, cur=FakeCur((0, 60000, False))) is None


class FakeLp:
    def __init__(self, market, source):
        self.market = market
        self.source = source


class Holder:
    def __init__(self):
        self._pool_fee_rates = {}
        self._pool_fee_miss_block = {}


def _resolve(holder, lp, blk):
    return state_mod.State._launchpad_pool_fee_rate(holder, lp, blk)


def test_resolved_rate_is_cached_and_not_requeried(monkeypatch):
    calls = []

    def fake(market, source, cur=None):
        calls.append(market)
        return Decimal("0.0135")

    monkeypatch.setattr(state_mod.storage, "get_pool_fee_rate", fake)
    holder, lp = Holder(), FakeLp("0xpair", 2)
    assert _resolve(holder, lp, 100) == Decimal("0.0135")
    assert _resolve(holder, lp, 200) == Decimal("0.0135")
    assert len(calls) == 1


def test_misses_back_off_then_retry(monkeypatch):
    calls = []

    def fake(market, source, cur=None):
        calls.append(market)
        return None

    monkeypatch.setattr(state_mod.storage, "get_pool_fee_rate", fake)
    holder, lp = Holder(), FakeLp("0xpair", 2)
    assert _resolve(holder, lp, 100) is None
    assert _resolve(holder, lp, 100 + state_mod.POOL_FEE_RETRY_BLOCKS - 1) is None
    assert len(calls) == 1
    assert _resolve(holder, lp, 100 + state_mod.POOL_FEE_RETRY_BLOCKS) is None
    assert len(calls) == 2


def test_storage_failure_is_swallowed(monkeypatch):
    def boom(market, source, cur=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(state_mod.storage, "get_pool_fee_rate", boom)
    assert _resolve(Holder(), FakeLp("0xpair", 2), 100) is None


def test_missing_market_needs_no_lookup(monkeypatch):
    def boom(market, source, cur=None):
        raise AssertionError("should not query")

    monkeypatch.setattr(state_mod.storage, "get_pool_fee_rate", boom)
    assert _resolve(Holder(), FakeLp("", 1), 100) is None
