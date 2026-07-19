"""stats endpoint shape and equivalence.

the frontend copies keys verbatim and reads them by string template, so the
rewrite must produce an identical payload to the per window implementation.
"""

import os
import sys
import time
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    _api_client,
    _create,
    _new_state,
    _trade,
    clean,  # noqa: F401  autouse truncation between tests
    db,  # noqa: F401
)


# the endpoint caches for 500ms, so tests hitting the same token in quick
# succession would otherwise read each other's responses
@pytest.fixture(autouse=True)
def _clear_api_cache():
    import api.api as api_mod

    api_mod._cache.clear()
    yield
    api_mod._cache.clear()


WINDOWS = ("5m", "1h", "6h", "24h")
BASES = (
    "volume_usd",
    "buy_volume_usd",
    "sell_volume_usd",
    "buy_tx_count",
    "sell_tx_count",
    "change_pct",
    "price_ref",
)


# every key the frontend templates must exist with the right type
def test_stats_exposes_every_windowed_key(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=now - 120, txh="0xs1", log_idx=0)
    _trade(st, native_reserve=1400 * 10**18, blk=102, ts=now - 30, txh="0xs2", log_idx=0)

    body = _api_client().get(f"/stats/{TOKEN}").json()

    assert body["type"] == "stats"
    assert body["token"] == TOKEN
    for w in WINDOWS:
        for b in BASES:
            key = f"{b}_{w}"
            assert key in body, f"missing {key}"
        assert isinstance(body[f"volume_usd_{w}"], float)
        assert isinstance(body[f"buy_tx_count_{w}"], int)
        assert isinstance(body[f"change_pct_{w}"], float)
        assert isinstance(body[f"price_ref_{w}"], str)


# windowed aggregates must actually respect their boundaries
def test_stats_windows_are_bounded_correctly(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())
    # one trade inside 5m, one only inside 6h
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=now - 60, txh="0xw1", log_idx=0)
    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=now - 3 * 3600, txh="0xw2", log_idx=0)

    body = _api_client().get(f"/stats/{TOKEN}").json()

    assert body["buy_tx_count_5m"] == 1, "only the recent trade is inside 5m"
    assert body["buy_tx_count_1h"] == 1
    assert body["buy_tx_count_6h"] == 2, "both trades are inside 6h"
    assert body["buy_tx_count_24h"] == 2
    assert body["volume_usd_6h"] >= body["volume_usd_5m"], "wider window cannot hold less"


# change_pct and price_ref must stay self consistent after the rewrite
def test_stats_change_pct_matches_its_reference_price(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())
    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=now - 3600, txh="0xc1", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, blk=102, ts=now - 10, txh="0xc2", log_idx=0)

    c = _api_client()
    body = c.get(f"/stats/{TOKEN}").json()
    last = Decimal(c.get(f"/token/{TOKEN}/60").json()["marketcap"])

    ref = Decimal(body["price_ref_24h"])
    assert ref > 0
    implied = float((last / ref - 1) * 100)
    assert abs(implied - body["change_pct_24h"]) < 0.01, (implied, body["change_pct_24h"])


# a token with no trades must not error or fabricate numbers
def test_stats_on_an_untraded_token_is_all_zero(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)

    body = _api_client().get(f"/stats/{TOKEN}").json()
    for w in WINDOWS:
        assert body[f"volume_usd_{w}"] == 0.0
        assert body[f"buy_tx_count_{w}"] == 0
        assert body[f"change_pct_{w}"] == 0.0
        assert body[f"price_ref_{w}"] == "0"
