import os
import sys
import threading
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod

POOL = "0xpool00000000000000000000000000000000001"
CLOB = "0xclob00000000000000000000000000000000002"
EMPTY = "0xempty0000000000000000000000000000000003"


class Mi:
    def __init__(self, market_type, rq, rb):
        self.marketType = market_type
        self.reserveQuote = rq
        self.reserveBase = rb
        self.quoteDecimals = 6
        self.baseDecimals = 18


class Stub:
    def __init__(self):
        self._lock = threading.Lock()
        self.addressToMarket = {
            POOL: Mi(2, 1_000_000, 5 * 10**18),
            CLOB: Mi(0, 999, 999),
            EMPTY: Mi(2, 0, 0),
        }

    def _pool_tvl_usd_locked(self, mi, rq, rb):
        return Decimal("42.5")

    record_pool_tvl_samples = state_mod.State.record_pool_tvl_samples


def test_only_amm_pools_with_reserves_are_sampled():
    stub = Stub()
    calls = []
    with patch.object(state_mod.storage, "insert_crystal_pool_tvl_sample", side_effect=lambda **kw: calls.append(kw)):
        written = stub.record_pool_tvl_samples(500, 1700)
    assert written == 1
    assert len(calls) == 1
    row = calls[0]
    assert row["market"] == POOL
    assert row["block_number"] == 500
    assert row["timestamp"] == 1700
    assert row["tvl_usd"] == Decimal("42.5")
    assert row["reserve_quote"] == 1_000_000


def test_timer_samples_use_a_sentinel_log_index_that_no_event_can_use():
    stub = Stub()
    calls = []
    with patch.object(state_mod.storage, "insert_crystal_pool_tvl_sample", side_effect=lambda **kw: calls.append(kw)):
        stub.record_pool_tvl_samples(1, 2)
    assert calls[0]["log_index"] == state_mod.POOL_SAMPLER_LOG_INDEX
    # a real log index is never negative, so the primary key can never collide
    assert state_mod.POOL_SAMPLER_LOG_INDEX < 0
    assert calls[0]["txhash"] == ""


def test_a_failing_pool_does_not_stop_the_rest():
    stub = Stub()
    stub.addressToMarket["0xsecond000000000000000000000000000000004"] = Mi(2, 7, 8)
    seen = []

    def flaky(**kw):
        seen.append(kw["market"])
        if len(seen) == 1:
            raise RuntimeError("db blip")

    with patch.object(state_mod.storage, "insert_crystal_pool_tvl_sample", side_effect=flaky):
        written = stub.record_pool_tvl_samples(3, 4)
    assert len(seen) == 2
    assert written == 1
