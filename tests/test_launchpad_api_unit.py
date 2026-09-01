from decimal import Decimal

import pytest
from fastapi import HTTPException

from api.api import _initial_price_kline
from api.routes import launchpad
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


def test_token_feeds_use_independent_rankings(monkeypatch):
    executed = []
    rows_by_query = []

    class Cursor:
        def execute(self, query, params):
            normalized = " ".join(query.split())
            executed.append((normalized, params))
            if "WITH volume_24h" in normalized:
                rows_by_query.append([("trend",)])
            elif "t.migrated = TRUE" in normalized:
                rows_by_query.append([("graduated",)])
            elif "t.circulating_supply DESC" in normalized:
                rows_by_query.append([("near",)])
            else:
                rows_by_query.append([("new",)])

        def fetchall(self):
            return rows_by_query.pop(0)

    class CursorContext:
        def __enter__(self):
            return Cursor()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(launchpad, "db_cursor", lambda: CursorContext())
    monkeypatch.setattr(
        launchpad,
        "_batch_serialize_tokens",
        lambda addresses: {
            address: {"token": address, "source": 0, "circulating_supply": "1"}
            for address in addresses
        },
    )
    monkeypatch.setattr(launchpad.storage, "get_last_processed_block", lambda: 123)

    body = launchpad.token_feeds(source="0", limit=7)

    assert [row["token"] for row in body["trending"]] == ["trend"]
    assert [row["token"] for row in body["new"]] == ["new"]
    assert [row["token"] for row in body["near_graduation"]] == ["near"]
    assert [row["token"] for row in body["graduated"]] == ["graduated"]
    assert body["as_of_block"] == 123

    queries = [query for query, _params in executed]
    assert "timestamp >= %s" in queries[0]
    assert "COALESCE(v.native_volume, 0) DESC" in queries[0]
    assert "t.created_at DESC" in queries[1]
    assert "t.circulating_supply DESC" in queries[2]
    assert "t.migrated_at DESC" in queries[3]
    assert all("t.source = 0" in query for query in queries)
