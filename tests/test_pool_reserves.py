"""live pool reserves replacing frozen curve reserves on graduated tokens."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.api  # noqa: F401,E402  (import order is load-bearing)


def test_live_pool_reserves_replace_frozen_curve_reserves(monkeypatch):
    import api.api as A

    token_data = {
        "0xaaa": {"migrated": True, "market": "0xm1", "reserveQuote": "1", "reserveBase": "2"},
        "0xbbb": {"migrated": True, "market": "0xm2", "reserveQuote": "3", "reserveBase": "4"},
        "0xccc": {"migrated": False, "market": None, "reserveQuote": "5", "reserveBase": "6"},
    }
    monkeypatch.setattr(
        A.storage,
        "pool_reserves_for_tokens",
        lambda toks: {"0xaaa": {"reserveNative": "111", "reserveToken": "222", "syncedAt": 9}},
    )
    monkeypatch.setattr(
        A.storage,
        "crystal_pool_reserves_for_markets",
        lambda mkts: {"0xm2": {"reserveQuote": "333", "reserveBase": "444", "syncedAt": 8}},
    )

    A._apply_live_pool_reserves(token_data)

    assert token_data["0xaaa"]["reserveQuote"] == "111"
    assert token_data["0xaaa"]["reserveBase"] == "222"
    assert token_data["0xaaa"]["reservesFrom"] == "pair"
    assert token_data["0xaaa"]["reservesSyncedAt"] == 9

    assert token_data["0xbbb"]["reserveQuote"] == "333"
    assert token_data["0xbbb"]["reserveBase"] == "444"
    assert token_data["0xbbb"]["reservesFrom"] == "crystal_pool"

    assert token_data["0xccc"]["reserveQuote"] == "5"
    assert token_data["0xccc"]["reserveBase"] == "6"
    assert "reservesFrom" not in token_data["0xccc"]


def test_live_pool_reserves_survive_lookup_failure(monkeypatch):
    import api.api as A

    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    token_data = {"0xaaa": {"migrated": True, "market": "0xm1", "reserveQuote": "7", "reserveBase": "8"}}
    monkeypatch.setattr(A.storage, "pool_reserves_for_tokens", boom)
    monkeypatch.setattr(A.storage, "crystal_pool_reserves_for_markets", boom)

    A._apply_live_pool_reserves(token_data)

    assert token_data["0xaaa"]["reserveQuote"] == "7"
    assert token_data["0xaaa"]["reserveBase"] == "8"


def test_live_pool_reserves_skips_when_none_migrated(monkeypatch):
    import api.api as A

    called = []
    monkeypatch.setattr(A.storage, "pool_reserves_for_tokens", lambda t: called.append(t) or {})
    token_data = {"0xccc": {"migrated": False, "reserveQuote": "5", "reserveBase": "6"}}
    A._apply_live_pool_reserves(token_data)
    assert called == []
