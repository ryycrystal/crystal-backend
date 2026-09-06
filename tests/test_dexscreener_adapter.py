from decimal import Decimal

import pytest
from fastapi import HTTPException

import api.api  # noqa: F401
from api.routes import dexscreener
from api.routes.dexscreener import (
    _addr_or_404,
    _checksum,
    _dec_str,
    _price_native,
    dex_asset,
    dex_events,
    dex_latest_block,
    dex_pair,
)


class _CursorContext:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def test_checksum_matches_eip55_vectors():
    assert _checksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert _checksum("0xFB6916095CA1DF60BB79CE92CE3EA74C37C5D359") == "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"
    assert _checksum("0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb") == "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB"


def test_addr_validation():
    assert _addr_or_404(" 0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed ") == "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    for bad in ["", "elonmusk", "0x123", "0x" + "z" * 40]:
        with pytest.raises(HTTPException) as exc_info:
            _addr_or_404(bad)
        assert exc_info.value.status_code == 404


def test_dec_str_plain_decimal_no_exponents():
    assert _dec_str(10**27) == "1000000000"
    assert _dec_str(1500000000000000000) == "1.5"
    assert _dec_str(1) == "0.000000000000000001"
    assert _dec_str(0) == "0"
    assert _dec_str(None) == "0"
    assert _dec_str(66666666666666666666666667) == "66666666.666666666666666667"
    for raw in [1, 10**27, 123456789, 10**45]:
        s = _dec_str(raw)
        assert "e" not in s.lower()
        assert Decimal(s) == Decimal(raw) / Decimal(10) ** 18


def test_price_native_matches_amount_ratio():
    native_amt = 2500000000000000000
    token_amt = 71428571428571428571428571
    p = _price_native(native_amt, token_amt, 0, 0)
    assert "e" not in p.lower()
    assert Decimal(p) > 0
    assert abs(Decimal(p) - Decimal(native_amt) / Decimal(token_amt)) < Decimal("1e-49")


def test_price_native_falls_back_to_reserves_then_declines_to_guess():
    from_reserves = _price_native(0, 0, 10**18, 10**27)
    assert Decimal(from_reserves) == Decimal("0.000000001")
    assert _price_native(0, 0, 0, 0) is None
    assert _price_native(1, 10**27, 0, 0) == "0.000000000000000000000000001"


def test_price_native_scales_a_six_decimal_quote():
    assert Decimal(_price_native(2_000_000, 10**18, 0, 0, quote_decimals=6)) == Decimal("2")
    assert _dec_str(2_500_000, 6) == "2.5"


def test_latest_block_reads_kv_checkpoint(monkeypatch):
    ctx = _CursorContext([[("dex_tip_block", "3141592"), ("dex_tip_ts", "1756900000")]])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    assert dex_latest_block() == {"block": {"blockNumber": 3141592, "blockTimestamp": 1756900000}}


def test_latest_block_falls_back_to_processed_blocks(monkeypatch):
    ctx = _CursorContext([[], (2718281,), (1756800000,)])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    assert dex_latest_block() == {"block": {"blockNumber": 2718281, "blockTimestamp": 1756800000}}


def test_asset_shape_and_404(monkeypatch):
    token = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    ctx = _CursorContext([("Test Token", "TST", 123456789 * 10**18)])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    body = dex_asset(id=token)
    assert body == {
        "asset": {
            "id": _checksum(token),
            "name": "Test Token",
            "symbol": "TST",
            "totalSupply": "1000000000",
            "circulatingSupply": "123456789",
        }
    }
    assert "source = 0" in ctx.executed[0][0]

    ctx = _CursorContext([None, None])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    with pytest.raises(HTTPException) as exc_info:
        dex_asset(id=token)
    assert exc_info.value.status_code == 404


def test_asset_serves_quote_token(monkeypatch):
    ctx = _CursorContext([None, (1,)])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    body = dex_asset(id=dexscreener.WMON)
    assert body["asset"]["name"] == "Wrapped Monad"
    assert body["asset"]["symbol"] == "WMON"
    assert body["asset"]["id"] == _checksum(dexscreener.WMON)

    ctx = _CursorContext([None, (1,)])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    assert dex_asset(id=dexscreener.USDC)["asset"]["symbol"] == "USDC"


def test_pair_shape_and_creator_omitted_when_empty(monkeypatch):
    token = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    creator = "0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb"
    ctx = _CursorContext([(creator, 100, 1756000000, dexscreener.WMON)])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    body = dex_pair(id=token)
    assert body == {
        "pair": {
            "id": _checksum(token),
            "dexKey": dexscreener.DEX_KEY,
            "asset0Id": _checksum(token),
            "asset1Id": _checksum(dexscreener.WMON),
            "createdAtBlockNumber": 100,
            "createdAtBlockTimestamp": 1756000000,
            "feeBps": dexscreener.FEE_BPS,
            "creator": _checksum(creator),
        }
    }

    ctx = _CursorContext([("", 100, 1756000000, dexscreener.WMON)])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    assert "creator" not in dex_pair(id=token)["pair"]


def test_events_swap_shape_and_direction(monkeypatch):
    token = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    maker = "0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb"
    rows = [
        (
            200,
            1756000100,
            "0xaaa",
            3,
            maker,
            token,
            True,
            10**18,
            4 * 10**25,
            11 * 10**18,
            9 * 10**26,
            5,
            dexscreener.WMON,
        ),
        (200, 1756000100, "0xaaa", 7, maker, token, False, 5 * 10**17, 2 * 10**25, 10 * 10**18, 92 * 10**25, 5, None),
    ]
    ctx = _CursorContext([rows])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    body = dex_events(fromBlock=200, toBlock=200)
    buy, sell = body["events"]

    assert buy["block"] == {"blockNumber": 200, "blockTimestamp": 1756000100}
    assert buy["eventType"] == "swap"
    assert (buy["txnId"], buy["txnIndex"], buy["eventIndex"]) == ("0xaaa", 5, 3)
    assert buy["maker"] == _checksum(maker)
    assert buy["pairId"] == _checksum(token)
    assert buy["asset1In"] == "1" and buy["asset0Out"] == "40000000"
    assert "asset0In" not in buy and "asset1Out" not in buy
    assert buy["reserves"] == {"asset0": "900000000", "asset1": "11"}
    assert Decimal(buy["priceNative"]) == Decimal(10**18) / Decimal(4 * 10**25)

    assert sell["asset0In"] == "20000000" and sell["asset1Out"] == "0.5"
    assert "asset1In" not in sell and "asset0Out" not in sell
    assert (sell["txnIndex"], sell["eventIndex"]) == (5, 7)

    query = ctx.executed[0][0]
    assert "ORDER BY t.block_number, t.log_index" in query
    assert "source = 0" in query
    assert "t.venue = 'curve'" in query
    assert "t.venue IS NULL" in query
    assert "migrated" in query


def test_events_skip_unpriceable_rows_and_default_txn_index(monkeypatch):
    token = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    maker = "0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb"
    rows = [
        (300, 1756000200, "0xbbb", 1, maker, token, True, 0, 0, 0, 0, None, dexscreener.WMON),
        (300, 1756000200, "0xbbb", 2, maker, token, True, 10**18, 10**24, 10**18, 10**26, None, dexscreener.WMON),
    ]
    ctx = _CursorContext([rows])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    events = dex_events(fromBlock=300, toBlock=300)["events"]
    assert [e["eventIndex"] for e in events] == [2]
    assert events[0]["txnIndex"] == 0


def test_events_use_quote_decimals_for_the_native_leg(monkeypatch):
    token = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
    maker = "0xdbf03b407c01e7cd3cbea99509d93f8dddc8c6fb"
    rows = [
        (400, 1756000300, "0xccc", 1, maker, token, True, 3_000_000, 10**18, 9_000_000, 5 * 10**18, 2, dexscreener.USDC)
    ]
    ctx = _CursorContext([rows])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    ev = dex_events(fromBlock=400, toBlock=400)["events"][0]
    assert ev["asset1In"] == "3"
    assert ev["asset0Out"] == "1"
    assert ev["reserves"] == {"asset0": "5", "asset1": "9"}
    assert Decimal(ev["priceNative"]) == Decimal("3")


def test_events_empty_and_inverted_range(monkeypatch):
    ctx = _CursorContext([[]])
    monkeypatch.setattr(dexscreener, "db_cursor", lambda: ctx)
    assert dex_events(fromBlock=1, toBlock=1) == {"events": []}
    assert dex_events(fromBlock=10, toBlock=5) == {"events": []}


def test_http_wiring(monkeypatch):
    from fastapi.testclient import TestClient

    from api.api import app

    client = TestClient(app)
    monkeypatch.setattr(
        dexscreener, "db_cursor", lambda: _CursorContext([[("dex_tip_block", "42"), ("dex_tip_ts", "1756000000")]])
    )
    resp = client.get("/dexscreener/latest-block")
    assert resp.status_code == 200
    assert resp.json() == {"block": {"blockNumber": 42, "blockTimestamp": 1756000000}}

    monkeypatch.setattr(dexscreener, "db_cursor", lambda: _CursorContext([None, None]))
    assert client.get("/dexscreener/asset?id=0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed").status_code == 404
    assert client.get("/dexscreener/asset?id=notanaddress").status_code == 404
    assert client.get("/dexscreener/events?fromBlock=1").status_code == 422
    assert client.get("/dexscreener/events?fromBlock=abc&toBlock=2").status_code == 422
