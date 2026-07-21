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


# the two filters the panel could not use, and the echo that makes a misspelled
# param visible instead of silently returning an unfiltered page
def test_search_supports_pro_traders_and_insider_filters(db, clean):
    from fastapi.testclient import TestClient

    import api.api

    client = TestClient(api.api.app)

    r = client.get("/search/query", params={"limit": 1, "pro_traders_min": 1})
    assert r.status_code == 200
    assert "pro_traders_min" in r.json()["applied_filters"]

    r = client.get("/search/query", params={"limit": 1, "insider_holding_min": 1})
    assert r.status_code == 200
    assert "insider_holding_min" in r.json()["applied_filters"]

    # a param the server does not know must not look like it was applied
    r = client.get("/search/query", params={"limit": 1, "volumeMin": 5})
    assert r.status_code == 200
    assert r.json()["applied_filters"] == [], "an unknown param must not report as applied"


# a row that matched a holder-derived filter must not render zero for the field the
# user filtered on. the batch serializer hardcoded snipers, so a search could return
# 25 rows above 5% that every one of them displayed as 0.00%
def test_list_rows_carry_the_fields_the_search_can_filter_on(db, clean):
    from api.api import _batch_get_holder_stats, _batch_serialize_tokens

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xsn1", log_idx=0)
    storage.record_block_processed(101)

    stats = _batch_get_holder_stats([TOKEN], set())
    assert TOKEN in stats
    # the three that used to be absent from the batch path entirely
    for key in ("sniper_count", "sniper_addresses", "sniper_holding", "insider_holding", "pro_traders"):
        assert key in stats[TOKEN], f"batch holder stats missing {key}"

    rows = _batch_serialize_tokens([TOKEN], set())
    row = rows[TOKEN]
    assert "snipers" in row and set(row["snipers"]) == {"count", "addresses", "holdingShare"}
    assert "insider_holding" in row, "insider_holding must be on the row, not hardcoded client side"
    assert "pro_traders" in row, "pro_traders must be on the row"


# holdingShare is a percent of the 1e27 supply, matching the filter's basis. it was
# divided by 1e9, which is neither a fraction nor a percent
def test_sniper_holding_share_is_a_percent_of_supply(db, clean):
    from decimal import Decimal as D

    from api.api import _PCT_OF_SUPPLY

    assert _PCT_OF_SUPPLY == D(10) ** 25
    # a wallet holding a tenth of the 1e27 supply is 10 percent
    assert float(D(10) ** 26 / _PCT_OF_SUPPLY) == 10.0


# a blacklist that only hides what is on the current page is not a blacklist. these
# have to exclude in sql, and the client's normalisation has to match the server's
def test_blacklist_exclusions_run_in_sql(db, clean):
    import core.storage as st

    _create(_new_state(), blk=100, ts=1000)
    with st.db_cursor() as cur:
        cur.execute(
            "UPDATE launchpad_tokens SET creator=%s, social1=%s, social2=%s WHERE token=%s",
            ("0xdead", "https://www.Evil.com/path", "https://x.com/BadGuy", TOKEN),
        )

    def total(ex):
        _, _, n = st.search_tokens_filtered(query="", filters=ex, limit=5, offset=0, mon_usd=1)
        return n

    assert total({}) >= 1
    assert total({"exclude_dev": ["0xDEAD"]}) == 0, "creator match is case insensitive"
    assert total({"exclude_ca": [TOKEN.upper()]}) == 0, "token match is case insensitive"
    # bare host, and a full url reduced to the same host
    assert total({"exclude_website": ["evil.com"]}) == 0
    assert total({"exclude_website": ["https://www.evil.com/other"]}) == 0
    # bare handle, @handle and a full url all reduce to the same thing
    for form in ("badguy", "@BadGuy", "https://x.com/BadGuy/status/9"):
        assert total({"exclude_twitter": [form]}) == 0, f"handle form {form!r} did not match"
    # a value that matches nothing must not exclude anything
    assert total({"exclude_website": ["notevil.com"]}) >= 1
    assert total({"exclude_twitter": ["someoneelse"]}) >= 1


# source 1 has to mean nad.fun regardless of generation, since v1 and v2 are both
# reported as 1 on the wire
def test_source_filter_covers_both_nadfun_generations(db, clean):
    import core.storage as st

    _create(_new_state(), blk=100, ts=1000)

    def total(src):
        _, _, n = st.search_tokens_filtered(query="", filters={"source": src}, limit=5, offset=0, mon_usd=1)
        return n

    with st.db_cursor() as cur:
        cur.execute("UPDATE launchpad_tokens SET source=2 WHERE token=%s", (TOKEN,))
    assert total(1) == 1, "a v2 token must match a nad.fun request"
    assert total(0) == 0

    with st.db_cursor() as cur:
        cur.execute("UPDATE launchpad_tokens SET source=0 WHERE token=%s", (TOKEN,))
    assert total(0) == 1
    assert total(1) == 0


# the documented 1000 entry blacklist is far past what a query string carries, so the
# post form has to accept the same filters and return the same shape
def test_search_post_matches_the_get_form(db, clean):
    from fastapi.testclient import TestClient

    import api.api

    client = TestClient(api.api.app)
    _create(_new_state(), blk=100, ts=1000)

    got = client.get("/search/query", params={"limit": 5})
    post = client.post("/search/query", json={"limit": 5})
    assert post.status_code == 200
    assert post.json()["total"] == got.json()["total"]
    assert set(post.json()) == set(got.json())

    # a list the query string could not carry
    big = ["0x" + f"{i:040x}" for i in range(1000)]
    r = client.post("/search/query", json={"limit": 1, "exclude_ca": big})
    assert r.status_code == 200
    assert "exclude_ca" in r.json()["applied_filters"]

    # and excluding a real token by post actually removes it
    r = client.post("/search/query", json={"limit": 5, "exclude_ca": [TOKEN]})
    assert all(row.get("token") != TOKEN for row in r.json()["results"])


# query travels outside the filters dict, so it has to be echoed explicitly. a client
# that asserts every param it sent came back would otherwise fall back on every search
def test_query_is_echoed_in_applied_filters(db, clean):
    from fastapi.testclient import TestClient

    import api.api

    client = TestClient(api.api.app)

    r = client.get("/search/query", params={"limit": 1, "query": "abc"})
    assert "query" in r.json()["applied_filters"]

    r = client.get("/search/query", params={"limit": 1})
    assert "query" not in r.json()["applied_filters"], "an empty query is not a filter"

    r = client.post("/search/query", json={"limit": 1, "query": "abc"})
    assert "query" in r.json()["applied_filters"], "post form must echo it too"


# change since launch is measured from the first recorded trade, always available for a
# token that has traded, and never dependent on v0 which is settable
def test_change_pct_since_launch(db, clean):
    from api.api import _batch_get_price_changes

    st = _new_state()
    _create(st, blk=100, ts=1000)
    # three trades: launch price rises, so a positive change since launch. reserves
    # climb, which raises last_price_native
    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=1001, txh="0xa", log_idx=0)
    _trade(st, native_reserve=1500 * 10**18, blk=102, ts=1002, txh="0xb", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, blk=103, ts=1003, txh="0xc", log_idx=0)

    ch = _batch_get_price_changes([TOKEN])[TOKEN]
    assert ch["change_pct_since_launch"] is not None
    # last price is above the first, so the change is positive
    assert Decimal(ch["change_pct_since_launch"]) > 0

    # a token that has never traded has no baseline -> null, which the card dashes
    _create(st, token="0x00000000000000000000000000000000000000ff", blk=100, ts=1000)
    ch2 = _batch_get_price_changes(["0x00000000000000000000000000000000000000ff"])
    assert ch2.get("0x00000000000000000000000000000000000000ff", {}).get("change_pct_since_launch") is None


# the launch reference is the EARLIEST trade, not the latest. an ascending vs
# descending mix-up would silently make every token read ~0% since launch
def test_launch_reference_is_the_first_trade(db, clean):
    from api.api import _batch_get_price_changes

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=1001, txh="0xf1", log_idx=0)
    early = _batch_get_price_changes([TOKEN])[TOKEN]["change_pct_since_launch"]
    # first trade == current, so change is 0 (or None if the single price is falsy)
    assert early in ("0", None)

    _trade(st, native_reserve=3000 * 10**18, blk=110, ts=1100, txh="0xf2", log_idx=0)
    later = _batch_get_price_changes([TOKEN])[TOKEN]["change_pct_since_launch"]
    # now the baseline is still the first trade, so a large positive change, not ~0
    assert Decimal(later) > 50, f"expected a big change from the first price, got {later}"
