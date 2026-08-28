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


def test_stats_windows_are_bounded_correctly(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=now - 60, txh="0xw1", log_idx=0)
    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=now - 3 * 3600, txh="0xw2", log_idx=0)

    body = _api_client().get(f"/stats/{TOKEN}").json()

    assert body["buy_tx_count_5m"] == 1, "only the recent trade is inside 5m"
    assert body["buy_tx_count_1h"] == 1
    assert body["buy_tx_count_6h"] == 2, "both trades are inside 6h"
    assert body["buy_tx_count_24h"] == 2
    assert body["volume_usd_6h"] >= body["volume_usd_5m"], "wider window cannot hold less"


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


def test_stats_on_an_untraded_token_is_all_zero(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)

    body = _api_client().get(f"/stats/{TOKEN}").json()
    for w in WINDOWS:
        assert body[f"volume_usd_{w}"] == 0.0
        assert body[f"buy_tx_count_{w}"] == 0
        assert body[f"change_pct_{w}"] == 0.0
        assert body[f"price_ref_{w}"] == "0"


def test_stats_watermarks_track_indexed_data(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())

    c = _api_client()

    body = c.get(f"/stats/{TOKEN}").json()
    assert body["as_of_ts"] == 0
    assert "as_of_block" in body

    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=now - 300, txh="0xwm1", log_idx=0)
    storage.record_block_processed(101)
    import api.api as api_mod

    api_mod._cache.clear()
    body = c.get(f"/stats/{TOKEN}").json()
    assert body["as_of_ts"] == now - 300, "watermark must equal the newest indexed trade"

    _trade(st, native_reserve=1200 * 10**18, blk=102, ts=now - 10, txh="0xwm2", log_idx=0)
    storage.record_block_processed(102)
    api_mod._cache.clear()
    body = c.get(f"/stats/{TOKEN}").json()
    assert body["as_of_ts"] == now - 10
    assert body["as_of_block"] >= 102, "block watermark must reach the processed block"


def test_as_of_block_disambiguates_same_second_trades(db):
    st = _new_state()
    _create(st, blk=100, ts=1000)
    now = int(time.time())

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

    assert body["as_of_ts"] == ts_after_first
    assert body["as_of_block"] > block_after_first
    assert body["buy_tx_count_5m"] == 2, "both trades are counted"


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
    assert "series" in slim and "klines" in slim["series"]
    assert slim["marketcap"] == full["marketcap"]
    assert len(slim["trades"]) == len(full["trades"])
    assert len(json.dumps(slim)) < len(json.dumps(full)), "slim payload must be smaller"


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

    scoped = c.get("/user", params={"addresses": USER, "token": TOKEN}).json()
    assert all((p.get("token") or "").lower() == TOKEN for p in scoped["users"][USER]["positions"])

    assert c.get("/user", params={"addresses": ""}).json()["count"] == 0
    assert c.get(f"/user/{USER}").status_code == 200


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

    r = client.get("/search/query", params={"limit": 1, "volumeMin": 5})
    assert r.status_code == 200
    assert r.json()["applied_filters"] == [], "an unknown param must not report as applied"


def test_list_rows_carry_the_fields_the_search_can_filter_on(db, clean):
    from api.api import _batch_get_holder_stats, _batch_serialize_tokens

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xsn1", log_idx=0)
    storage.record_block_processed(101)

    stats = _batch_get_holder_stats([TOKEN], set())
    assert TOKEN in stats
    for key in ("sniper_count", "sniper_addresses", "sniper_holding", "insider_holding", "pro_traders"):
        assert key in stats[TOKEN], f"batch holder stats missing {key}"

    rows = _batch_serialize_tokens([TOKEN], set())
    row = rows[TOKEN]
    assert "snipers" in row and set(row["snipers"]) == {"count", "addresses", "holdingShare"}
    assert "insider_holding" in row, "insider_holding must be on the row, not hardcoded client side"
    assert "pro_traders" in row, "pro_traders must be on the row"


def test_sniper_holding_share_is_a_percent_of_supply(db, clean):
    from decimal import Decimal as D

    from api.api import _PCT_OF_SUPPLY

    assert _PCT_OF_SUPPLY == D(10) ** 25
    assert float(D(10) ** 26 / _PCT_OF_SUPPLY) == 10.0


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
    assert total({"exclude_website": ["evil.com"]}) == 0
    assert total({"exclude_website": ["https://www.evil.com/other"]}) == 0
    for form in ("badguy", "@BadGuy", "https://x.com/BadGuy/status/9"):
        assert total({"exclude_twitter": [form]}) == 0, f"handle form {form!r} did not match"
    assert total({"exclude_website": ["notevil.com"]}) >= 1
    assert total({"exclude_twitter": ["someoneelse"]}) >= 1


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

    big = ["0x" + f"{i:040x}" for i in range(1000)]
    r = client.post("/search/query", json={"limit": 1, "exclude_ca": big})
    assert r.status_code == 200
    assert "exclude_ca" in r.json()["applied_filters"]

    r = client.post("/search/query", json={"limit": 5, "exclude_ca": [TOKEN]})
    assert all(row.get("token") != TOKEN for row in r.json()["results"])


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


def test_change_pct_since_launch(db, clean):
    from api.api import _batch_get_price_changes

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=1001, txh="0xa", log_idx=0)
    _trade(st, native_reserve=1500 * 10**18, blk=102, ts=1002, txh="0xb", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, blk=103, ts=1003, txh="0xc", log_idx=0)

    ch = _batch_get_price_changes([TOKEN])[TOKEN]
    assert ch["change_pct_since_launch"] is not None
    assert Decimal(ch["change_pct_since_launch"]) > 0

    _create(st, token="0x00000000000000000000000000000000000000ff", blk=100, ts=1000)
    ch2 = _batch_get_price_changes(["0x00000000000000000000000000000000000000ff"])
    assert ch2.get("0x00000000000000000000000000000000000000ff", {}).get("change_pct_since_launch") is None


def test_launch_reference_is_the_first_trade(db, clean):
    from api.api import _batch_get_price_changes

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1000 * 10**18, blk=101, ts=1001, txh="0xf1", log_idx=0)
    early = _batch_get_price_changes([TOKEN])[TOKEN]["change_pct_since_launch"]
    assert early in ("0", None)

    _trade(st, native_reserve=3000 * 10**18, blk=110, ts=1100, txh="0xf2", log_idx=0)
    later = _batch_get_price_changes([TOKEN])[TOKEN]["change_pct_since_launch"]
    assert Decimal(later) > 50, f"expected a big change from the first price, got {later}"


def test_token_meta_dumps_everything(db, clean, monkeypatch):
    from fastapi.testclient import TestClient

    import api.api
    import modules.nadfun as mn

    st = _new_state()
    _create(st, blk=100, ts=1000)
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1001, txh="0xmeta1", log_idx=0)
    storage.record_block_processed(101)

    monkeypatch.setattr(
        mn,
        "fetch_pair_fee_config",
        lambda pair: {
            "pair": pair,
            "ok": True,
            "fee_collector": "0xfc",
            "base_token": "0xbb",
            "quote_token": "0xqq",
            "creator_fee_rate": 100,
            "curve_protocol_fee_rate": 40,
            "dex_protocol_fee_rate": 60,
        },
    )
    client = TestClient(api.api.app)
    r = client.get(f"/token/{TOKEN}/meta")
    assert r.status_code == 200, r.text
    d = r.json()
    for col in ("token", "creator", "source", "circulating_supply", "native_volume", "tx_count"):
        assert col in d["raw"], f"raw dump missing column {col}"
    assert d["sourceRaw"] == 0 and d["source"] == 0
    assert "phase" in d and "progressBps" in d and "as_of_block" in d
    assert d["fees"]["curveFeeRate"] is None, "native curve fee is not a nadfun rate"
    assert d["fees"]["pair"] is None, "no market -> no pair fees"

    r404 = client.get("/token/0x00000000000000000000000000000000000000aa/meta")
    assert r404.status_code == 404


def test_pair_fees_cached_and_served(db, clean, monkeypatch):
    from fastapi.testclient import TestClient

    import api.api
    import modules.nadfun as mn

    calls = []

    def fake_fetch(pair):
        calls.append(pair)
        return {
            "pair": pair,
            "ok": True,
            "fee_collector": "0xfeec",
            "base_token": "0xb1",
            "quote_token": "0xq1",
            "creator_fee_rate": 100,
            "curve_protocol_fee_rate": 40,
            "dex_protocol_fee_rate": 60,
        }

    monkeypatch.setattr(mn, "fetch_pair_fee_config", fake_fetch)
    client = TestClient(api.api.app)
    pair = "0x" + "ab" * 20

    r1 = client.get(f"/pair/{pair}/fees")
    assert r1.status_code == 200
    d = r1.json()
    assert d["ok"] is True and d["dexProtocolFeeRate"] == "60" and d["creatorFeeRate"] == "100"

    r2 = client.get(f"/pair/{pair}/fees")
    assert r2.json() == d
    assert len(calls) == 1, "second read must come from the cache, not the chain"

    assert client.get("/pair/notanaddress/fees").status_code == 400


def test_preload_v2_token_persists_source_2(db, clean):
    import core.storage as st

    tok = "0x00000000000000000000000000000000000000e2"
    state = _new_state()
    state.ensure_v2_launchpad_token(tok, blk=100, ts=1000)
    with st.db_cursor() as cur:
        cur.execute("SELECT source FROM launchpad_tokens WHERE token = %s", (tok,))
        row = cur.fetchone()
    assert row and int(row[0]) == 2, f"preload wrote source={row and row[0]}, expected 2"


def test_nadfun_version_marker_fallback(db, clean):
    import api.api as a
    import core.storage as st

    tok = "0x00000000000000000000000000000000000000cd"
    st.mark_nadfun_v2(tok)
    a._nadfun_v2_cache = None
    assert a._nadfun_version(tok, 1) == 2, "marked token with stale source=1 must read v2"
    assert a._nadfun_version("0x00000000000000000000000000000000000000ce", 1) == 1
    assert a._nadfun_version(tok, 2) == 2


def test_klines_stitch_open_to_previous_close(db, clean):
    from decimal import Decimal as D

    from api.api import _build_ohlcv_from_db

    st = _new_state()
    _create(st, blk=100, ts=1000)
    with storage.db_cursor() as cur:
        cur.execute("DELETE FROM launchpad_ohlcv WHERE token = %s", (TOKEN,))
    _trade(st, native_reserve=1100 * 10**18, blk=101, ts=1000, txh="0xk1", log_idx=0)
    _trade(st, native_reserve=2000 * 10**18, blk=102, ts=1060, txh="0xk2", log_idx=0)

    ks = _build_ohlcv_from_db(TOKEN, bucket_seconds=60)
    assert len(ks) == 2
    first, second = ks
    assert second["open"] == first["close"], "open must stitch to the previous close"
    assert D(second["high"]) >= max(D(second["open"]), D(second["close"]))
    assert D(second["low"]) <= min(D(second["open"]), D(second["close"]))
    assert D(second["close"]) != D(second["open"]), "a real move is no longer a flat doji"


def test_spot_portfolio_one_call(db, clean, monkeypatch):
    from fastapi.testclient import TestClient

    import api.api
    import api.spot_data as sd

    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO crystal_markets (market, is_canonical, quote_asset, base_asset,
                quote_address, quote_decimals, quote_ticker, quote_name,
                base_address, base_decimals, base_ticker, base_name, last_price, updated_at)
            VALUES ('0xmkt1', TRUE, 'q', 'b',
                %s, 18, 'WMON', 'Wrapped Monad',
                '0x00000000000000000000000000000000000000b1', 18, 'ABC', 'Abc Token', 2.5, 100)
            ON CONFLICT (market) DO UPDATE SET last_price = EXCLUDED.last_price
            """,
            (sd.WMON,),
        )

    def fake_balances(wallet, tokens):
        return 12345, {"0x00000000000000000000000000000000000000b1": 4 * 10**18, sd.WMON: 0}, 2 * 10**18, False

    monkeypatch.setattr(sd, "fetch_balances", fake_balances)
    monkeypatch.setattr(sd, "wallet_is_supported", lambda w: True)
    monkeypatch.setattr(api.api, "_mon_price_usd", lambda: Decimal(3))
    import api.routes.launchpad as rl

    monkeypatch.setattr(rl, "_mon_price_usd", lambda: Decimal(3))

    client = TestClient(api.api.app)
    r = client.get("/spot/0x" + "ab" * 20)
    assert r.status_code == 200, r.text
    d = r.json()
    rows = {row["address"]: row for row in d["rows"]}
    assert rows["0x00000000000000000000000000000000000000b1"]["valueUsd"] == "30"
    assert rows["native"]["valueUsd"] == "6"
    assert d["summary"]["totalAccountValue"] == "36"
    assert sd.WMON not in rows
    assert d["summary"]["activeOrders"] is None and d["summary"]["totalVolume"] is None
    assert d["balance_block"] == 12345 and d["stale"] is False
    assert client.get("/spot/nope").status_code == 400


_OB_MARKET = "0x000000000000000000000000c8045b5dde24e625932df738e7ec4127c04008d3"
_OB_USER = "0x000000000000000000000000581172970bda012d71a9aea34a9f219da117891b"


def test_orderbook_orders_updated_decodes_real_mainnet_log(db):
    from modules.orderbook import ORDERS_UPDATED_TOPIC, parse_orders_updated

    def w(v):
        return f"{v:064x}"

    entries = [
        (1 << 252) | (26656000 << 168) | (13194139533313 << 112) | 362560565879531175936,
        (0 << 252) | (26611000 << 168) | (10995116277761 << 112) | 7578638,
        (2 << 252) | (26656000 << 168) | (2199023255553 << 112) | 4330650,
        (3 << 252) | (26677000 << 168) | (13194139533313 << 112) | 362560565879531241472,
        (4 << 252) | (26611000 << 168) | (10995116277761 << 112) | 1000,
    ]
    data = "0x" + w(0x20) + w(len(entries) * 32) + "".join(w(e) for e in entries)

    u = parse_orders_updated("0xrouter", [ORDERS_UPDATED_TOPIC, _OB_MARKET, _OB_USER], data)
    assert u["market"] == "0xc8045b5dde24e625932df738e7ec4127c04008d3"
    assert u["user"] == "0x581172970bda012d71a9aea34a9f219da117891b"
    assert len(u["orders"]) == 5

    o0, o1, o2, o3, o4 = u["orders"]
    assert o0 == {
        "flag": 1,
        "action": "remove",
        "is_buy": False,
        "price": 26656000,
        "order_id": 13194139533313,
        "size": 362560565879531175936,
    }, "the exact values observed on mainnet"
    assert o1["action"] == "remove" and o1["is_buy"] is True and o1["size"] == 7578638
    assert o2["action"] == "add" and o2["is_buy"] is True and o2["order_id"] == 2199023255553
    assert o3["action"] == "add" and o3["is_buy"] is False and o3["order_id"] == o0["order_id"], (
        "a requote re-adds the same order id it removed"
    )
    assert o4["action"] == "decrease" and o4["is_buy"] is True and o4["size"] == 1000


def test_orderbook_fill_decoder_layout(db):
    from modules.orderbook import FILL_TOPIC, parse_fill

    def w(v):
        return f"{v:064x}"

    info = (1 << 252) | (26656000 << 168) | (42 << 112) | 999
    amount = (1000 << 128) | 990
    f = parse_fill("0xrouter", [FILL_TOPIC, _OB_MARKET, _OB_USER], "0x" + w(info) + w(amount))
    assert f["market"] == "0xc8045b5dde24e625932df738e7ec4127c04008d3"
    assert f["maker"] == "0x581172970bda012d71a9aea34a9f219da117891b"
    assert f["maker_is_buy"] is True and f["price"] == 26656000 and f["order_id"] == 42
    assert f["remaining"] == 999
    assert f["amount_high"] == 1000 and f["amount_out"] == 990


def test_fork_guards_halt_on_divergence(db, clean, monkeypatch):
    import asyncio

    import backfill
    import core.storage as st

    async def fake_rpc_factory(chain_id_hex, tip_hash):
        async def fake(method, params):
            if method == "eth_chainId":
                return {"result": chain_id_hex}
            if method == "eth_getBlockByNumber":
                return {"result": {"hash": tip_hash}}
            raise AssertionError(method)

        return fake

    async def run(chain_id_hex="0x8f", head=1000, tip_hash="0xabc"):
        monkeypatch.setattr(backfill, "http_jsonrpc", await fake_rpc_factory(chain_id_hex, tip_hash))

        async def fake_head():
            return head

        monkeypatch.setattr(backfill, "get_head_http", fake_head)
        await backfill.verify_chain_continuity(st)

    st.record_block_processed(900)
    st.record_chain_tip(900, "0xabc")
    asyncio.run(run())

    try:
        asyncio.run(run(chain_id_hex="0x1"))
        raise AssertionError("wrong chain id must halt")
    except RuntimeError as e:
        assert "chain id" in str(e)

    try:
        asyncio.run(run(head=100))
        raise AssertionError("rolled back head must halt")
    except RuntimeError as e:
        assert "rolled back" in str(e)

    try:
        asyncio.run(run(tip_hash="0xdifferent"))
        raise AssertionError("tip hash mismatch must halt")
    except RuntimeError as e:
        assert "rewritten" in str(e)
