from decimal import Decimal

import pytest
from fastapi import HTTPException

from api.api import _initial_price_kline
from api.routes.launchpad import _normalize_source_filter, _source_where


def test_initial_price_kline_is_flat_and_bucketed():
    point = _initial_price_kline(125, Decimal("0.000001"), 60)

    assert point == {
        "time": "120",
        "open": "1000",
        "high": "1000",
        "low": "1000",
        "close": "1000",
        "quoteVolume": "0",
    }
    assert _initial_price_kline(125, Decimal("0.000001"), 60, before_ts=120) is None


def test_native_source_filter_accepts_numeric_zero():
    assert _normalize_source_filter(0) == "0"
    assert _source_where(0) == (["source = 0"], [])
    assert _source_where("1", "t") == (["t.source <> 0"], [])


@pytest.mark.parametrize("source", ["crystal", -1, 2])
def test_source_filter_rejects_unknown_values(source):
    with pytest.raises(HTTPException) as exc_info:
        _normalize_source_filter(source)
    assert exc_info.value.status_code == 400
