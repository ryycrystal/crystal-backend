import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.routes import orderbook as ob

MARKET = "0x" + "a" * 40


def test_every_chart_interval_is_a_supported_resolution():
    assert ob._KLINE_RESOLUTIONS == (60, 300, 900, 3600, 14400, 86400)


def test_rejects_a_malformed_market_address():
    for bad in ("", "0x123", "a" * 42, MARKET + "ff"):
        with pytest.raises(HTTPException) as e:
            ob.market_klines(bad)
        assert e.value.status_code == 400


def test_rejects_an_unsupported_resolution():
    with pytest.raises(HTTPException) as e:
        ob.market_klines(MARKET, res=7)
    assert e.value.status_code == 400


def test_payload_carries_the_fields_the_chart_reads(monkeypatch):
    row = {
        "time": 1700000000,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "baseVolume": "12345",
        "quoteVolume": "6789",
    }
    monkeypatch.setattr(ob, "_ensure_fresh", lambda: None)
    monkeypatch.setattr(ob.storage, "market_klines", lambda m, r, lim: [row])
    monkeypatch.setattr(ob.storage, "get_last_processed_block", lambda: 123)

    out = ob.market_klines(MARKET.upper(), res=60, limit=10)
    assert out["market"] == MARKET
    assert out["res"] == 60
    assert out["count"] == 1
    assert out["as_of_block"] == 123
    for field in ("time", "open", "high", "low", "close", "baseVolume"):
        assert field in out["klines"][0]


def test_limit_is_clamped_before_hitting_storage(monkeypatch):
    seen = {}

    def fake(m, r, lim):
        seen["lim"] = lim
        return []

    monkeypatch.setattr(ob, "_ensure_fresh", lambda: None)
    monkeypatch.setattr(ob.storage, "market_klines", fake)
    monkeypatch.setattr(ob.storage, "get_last_processed_block", lambda: 0)

    ob.market_klines(MARKET, res=60, limit=999999)
    assert seen["lim"] == 3000
    ob.market_klines(MARKET, res=60, limit=0)
    assert seen["lim"] == 3000
    ob.market_klines(MARKET, res=60, limit=-5)
    assert seen["lim"] == 1
