"""stats endpoint shape and equivalence.

the frontend copies keys verbatim and reads them by string template, so the
rewrite must produce an identical payload to the per window implementation.
"""

import json
import os
import sys
import time
from decimal import Decimal

import pytest

import core.storage as storage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    USER,
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


# the watermark tells a client which live trades are already counted here
def test_stats_watermarks_track_indexed_data(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())

    c = _api_client()

    # no trades: watermark is zero, not a fabricated "now"
    body = c.get(f"/stats/{TOKEN}").json()
    assert body["as_of_ts"] == 0
    assert "as_of_block" in body

    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=now - 300, txh="0xwm1", log_idx=0)
    storage.record_block_processed(101)
    import api.api as api_mod

    api_mod._cache.clear()
    body = c.get(f"/stats/{TOKEN}").json()
    assert body["as_of_ts"] == now - 300, "watermark must equal the newest indexed trade"

    # a newer trade advances it
    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=now - 10, txh="0xwm2", log_idx=0)
    storage.record_block_processed(102)
    api_mod._cache.clear()
    body = c.get(f"/stats/{TOKEN}").json()
    assert body["as_of_ts"] == now - 10
    assert body["as_of_block"] >= 102, "block watermark must reach the processed block"


# same-second trades are why as_of_block exists: as_of_ts alone cannot separate them
def test_as_of_block_disambiguates_same_second_trades(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())

    # two trades sharing a timestamp, in different blocks
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=now - 5, txh="0xss1", log_idx=0)
    storage.record_block_processed(101)
    c = _api_client()
    body = c.get(f"/stats/{TOKEN}").json()
    ts_after_first = body["as_of_ts"]
    block_after_first = body["as_of_block"]

    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=now - 5, txh="0xss2", log_idx=0)
    storage.record_block_processed(102)
    import api.api as api_mod

    api_mod._cache.clear()
    body = c.get(f"/stats/{TOKEN}").json()

    # the timestamp cannot tell the two apart...
    assert body["as_of_ts"] == ts_after_first
    # ...but the block watermark advanced, so a client keying on block stays exact
    assert body["as_of_block"] > block_after_first
    assert body["buy_tx_count_5m"] == 2, "both trades are counted"


# series is roughly half the token payload and the client discards it after first load
def test_series_can_be_omitted(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())
    for i in range(6):
        _trade(
            st, native_reserve=(1100 + i * 40) * 10**18, blk=101 + i, ts=now - 600 + i * 60, txh=f"0xser{i}", log_idx=0
        )

    c = _api_client()
    full = c.get(f"/token/{TOKEN}/60").json()
    slim = c.get(f"/token/{TOKEN}/60", params={"series": "false"}).json()

    assert len(full["series"]["klines"]) > 0, "default must still include the chart"
    assert slim["series"]["klines"] == [], "series=false must drop the bars"
    # the key stays present so the client shape never changes
    assert "series" in slim and "klines" in slim["series"]
    # everything else survives
    assert slim["marketcap"] == full["marketcap"]
    assert len(slim["trades"]) == len(full["trades"])
    assert len(json.dumps(slim)) < len(json.dumps(full)), "slim payload must be smaller"


# one request for many wallets instead of N
def test_batch_user_endpoint(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xbu1", log_idx=0)

    c = _api_client()
    other = "0x000000000000000000000000000000000000dead"
    body = c.get("/user", params={"addresses": f"{USER},{other},{USER}"}).json()

    assert body["count"] == 2, "duplicates must collapse"
    assert USER in body["users"]
    assert other in body["users"]
    assert "summary" in body["users"][USER]
    assert "positions" in body["users"][USER]

    # token filter narrows the positions
    scoped = c.get("/user", params={"addresses": USER, "token": TOKEN}).json()
    assert all((p.get("token") or "").lower() == TOKEN for p in scoped["users"][USER]["positions"])

    # empty input is not an error
    assert c.get("/user", params={"addresses": ""}).json()["count"] == 0
    # and the per-wallet route still works
    assert c.get(f"/user/{USER}").status_code == 200
