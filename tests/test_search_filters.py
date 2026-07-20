"""server side search filters and the /tokens delta parameter.

the defect being fixed: filters applied to an already truncated page hide a token
that matches but ranks 51st, which is wrong rather than merely incomplete.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import core.storage as storage  # noqa: E402
from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    _api_client,
    _create,
    _new_state,
    _trade,
    _x,
    clean,  # noqa: F401
    db,  # noqa: F401
)


@pytest.fixture(autouse=True)
def _clear_cache():
    import api.api as api_mod

    api_mod._cache.clear()
    yield
    api_mod._cache.clear()


def _mk(st, addr, name, symbol, blk, ts):
    _create(st, token=addr, blk=blk, ts=ts, name=name, symbol=symbol)


# query is optional so the explorer can browse by filter with no search term
def test_query_is_optional(db):
    st = _new_state()
    _mk(st, TOKEN, "Alpha", "ALP", 100, 1000)

    body = _api_client().get("/search/query").json()
    assert body["total"] >= 1
    assert body["count"] >= 1


# total reflects every match, not the returned page
def test_total_counts_all_matches_not_the_page(db):
    st = _new_state()
    now = int(time.time())
    for i in range(12):
        addr = "0x" + f"{i:040x}"
        _mk(st, addr, f"Token{i}", f"TK{i}", 100 + i, now - i)

    body = _api_client().get("/search/query", params={"limit": 5}).json()
    assert body["count"] == 5, "page is capped"
    assert body["total"] >= 12, "total must count beyond the page"


# a filter must reach tokens outside the first page, which is the whole point
def test_filters_run_in_sql_not_on_the_page(db):
    st = _new_state()
    now = int(time.time())
    # 12 tokens, only the last one carries a distinctive name
    for i in range(11):
        _mk(st, "0x" + f"{i:040x}", f"Common{i}", f"C{i}", 100 + i, now - 100 + i)
    needle = "0x" + f"{99:040x}"
    _mk(st, needle, "Zebra", "ZBR", 90, now - 500)

    c = _api_client()
    # with a small page and default recency order, Zebra is not on page one
    page = c.get("/search/query", params={"limit": 3}).json()
    assert needle not in [r["token"] for r in page["results"]]

    # but a keyword filter still finds it
    hit = c.get("/search/query", params={"keywords": "zebra", "limit": 3}).json()
    assert hit["total"] == 1
    assert hit["results"][0]["token"] == needle


# numeric ranges narrow the set
def test_numeric_range_filters(db):
    st = _new_state()
    now = int(time.time())
    _mk(st, TOKEN, "Traded", "TRD", 100, now - 60)
    for i in range(4):
        _trade(st, native_reserve=(1100 + i * 30) * 10**18, blk=101 + i, ts=now - 50 + i, txh=f"0xsf{i}", log_idx=0)

    c = _api_client()
    assert c.get("/search/query", params={"buy_tx_min": 1}).json()["total"] >= 1
    assert c.get("/search/query", params={"buy_tx_min": 999}).json()["total"] == 0

    # balances come from Transfer events, which the synthetic trade path does not
    # emit, so seed one directly to exercise the holder derived filters
    _x(db, "UPDATE launchpad_positions SET balance_token = %s WHERE token = %s", (5 * 10**25, TOKEN))
    import api.api as api_mod

    api_mod._cache.clear()
    assert c.get("/search/query", params={"holders_min": 1}).json()["total"] >= 1
    api_mod._cache.clear()
    assert c.get("/search/query", params={"holders_max": 0}).json()["total"] == 0
    # 5e25 of a 1e27 supply is 5 percent
    api_mod._cache.clear()
    assert c.get("/search/query", params={"top10_min": 4, "top10_max": 6}).json()["total"] >= 1
    api_mod._cache.clear()
    assert c.get("/search/query", params={"top10_min": 50}).json()["total"] == 0


# exclude_keywords removes matches rather than filtering to them
def test_exclude_keywords(db):
    st = _new_state()
    now = int(time.time())
    _mk(st, "0x" + f"{1:040x}", "KeepMe", "KEEP", 100, now)
    _mk(st, "0x" + f"{2:040x}", "DropMe", "DROP", 101, now)

    c = _api_client()
    body = c.get("/search/query", params={"exclude_keywords": "drop"}).json()
    names = [r["symbol"] for r in body["results"]]
    assert "KEEP" in names
    assert "DROP" not in names


# phase maps onto stored columns since it is derived, not a column
def test_phase_filter(db):
    st = _new_state()
    now = int(time.time())
    _mk(st, TOKEN, "New", "NEW", 100, now)

    c = _api_client()
    assert c.get("/search/query", params={"phase": "new"}).json()["total"] >= 1
    assert c.get("/search/query", params={"phase": "graduated"}).json()["total"] == 0


# /tokens?since_block returns only what moved, plus full membership so the client
# can drop what left
def test_tokens_since_returns_only_changed(db):
    st = _new_state()
    now = int(time.time())
    _mk(st, TOKEN, "Mover", "MOV", 100, now - 10)
    _trade(st, native_reserve=1100 * 10**18, blk=200, ts=now - 5, txh="0xsb1", log_idx=0)
    storage.record_block_processed(200)

    c = _api_client()
    full = c.get("/tokens").json()
    assert "ids" not in full, "the default response shape must be unchanged"

    import api.api as api_mod

    api_mod._cache.clear()
    delta = c.get("/tokens", params={"since_block": 100}).json()
    assert "ids" in delta, "membership must be returned so removals are derivable"
    assert delta["since_block"] == 100
    changed = [r["token"] for b in ("recent_created", "recent_approaching", "recent_graduated") for r in delta[b]]
    assert TOKEN in changed, "a token traded after the watermark must appear"

    api_mod._cache.clear()
    nothing = c.get("/tokens", params={"since_block": 999999}).json()
    later = [r["token"] for b in ("recent_created", "recent_approaching", "recent_graduated") for r in nothing[b]]
    assert later == [], "nothing changed after a future block"
    assert nothing["ids"]["recent_created"], "membership is still returned"
