"""spot graph bucket cache against a real database.

the properties that matter: buckets derive their block and mon/usd mapping from
the indexed trades, a filled bucket is immutable, and completeness is judged
against the same grid the filler uses, so complete never lies.
"""

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import api.api  # noqa: E402, F401
import core.storage as storage  # noqa: E402
from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    USER,
    clean,  # noqa: F401
    db,  # noqa: F401
)

WALLET = "0x00aa00bb00cc00dd00ee00ff00aa00bb00cc00dd"
POOL = "0x1111111111111111111111111111111111111111"
NOW = 1_800_000_000


class _FakeTime:
    @staticmethod
    def time() -> int:
        return NOW

    @staticmethod
    def sleep(_s: float) -> None:
        return


def _seed_trade(txh: str, block: int, ts: int, usd: str) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_trades
                (block_number, log_index, timestamp, token, user_address, is_buy,
                 native_amount, token_amount, usd_amount, price_native, txhash)
            VALUES (%s, 0, %s, %s, %s, true, %s, %s, %s, 0, %s)
            """,
            (block, ts, TOKEN, USER, 10**18, 10**20, Decimal(usd), txh),
        )


def _setup(monkeypatch, balance_wei: int):
    import api.spot_graph as sg

    monkeypatch.setattr(sg, "time", _FakeTime)
    monkeypatch.setattr(sg.storage, "wallet_has_crystal_activity", lambda w: True)
    _seed_trade("0xg1", 1000, NOW - 3 * 3600 + 100, "2.0")
    _seed_trade("0xg2", 2000, NOW - 3600 - 100, "3.0")
    monkeypatch.setattr(
        sg,
        "_balances_at_many",
        lambda wallet, pairs, tokens, lp_markets: {ts: {"native": balance_wei} for ts, _b in pairs},
    )
    return sg


def test_fill_covers_grid_and_prices_at_bucket_time(db, monkeypatch):
    sg = _setup(monkeypatch, 5 * 10**18)
    sg._fill(WALLET)

    wanted = sg._wanted_buckets(NOW)
    rows = storage.get_spot_graph_buckets(WALLET, 0)
    assert len(rows) == len(wanted), "every knowable bucket must be filled"

    newest_ts, newest_usd, _n = rows[-1]
    assert newest_ts == max(wanted)
    assert newest_usd == Decimal(5) * Decimal(3), "5 mon at the 3 usd oracle print"

    body = sg.graph_for(WALLET)
    assert body["complete"] is True
    assert body["points"][-1]["v"] == 15.0


def test_buckets_are_immutable(db, monkeypatch):
    sg = _setup(monkeypatch, 5 * 10**18)
    sg._fill(WALLET)
    before = storage.get_spot_graph_buckets(WALLET, 0)

    monkeypatch.setattr(
        sg, "_balances_at_many", lambda wallet, pairs, tokens, lp_markets: {ts: {"native": 1} for ts, _b in pairs}
    )
    sg._fill(WALLET)
    after = storage.get_spot_graph_buckets(WALLET, 0)

    assert after == before, "a second fill must not change or duplicate rows"


def test_fill_includes_lp_value_and_replaces_old_cache(db, monkeypatch):
    sg = _setup(monkeypatch, 0)
    wanted = sg._wanted_buckets(NOW)
    stale_ts = min(wanted)
    storage.write_spot_graph_bucket(WALLET, stale_ts, 1, Decimal(1), Decimal(0), {"native": "0"})
    storage.insert_crystal_pool_tvl_sample(
        market=POOL,
        block_number=1,
        log_index=0,
        timestamp=NOW - 4 * 3600,
        reserve_quote=0,
        reserve_base=0,
        tvl_usd=Decimal(400),
        txhash="0xlptvl",
    )
    monkeypatch.setattr(sg.storage, "list_lp_markets_for_graph", lambda wallet: [POOL])
    monkeypatch.setattr(
        sg,
        "_balances_at_many",
        lambda wallet, pairs, tokens, lp_markets: {
            ts: {
                "native": 0,
                f"{sg._LP_BALANCE_PREFIX}{POOL}": 25,
                f"{sg._LP_SUPPLY_PREFIX}{POOL}": 100,
            }
            for ts, _block in pairs
        },
    )

    sg._fill(WALLET)

    rows = storage.get_spot_graph_buckets(WALLET, 0, sg.VALUE_VERSION)
    assert len(rows) == len(wanted)
    assert all(usd == Decimal(100) for _ts, usd, _native in rows)
    assert dict((ts, usd) for ts, usd, _native in rows)[stale_ts] == Decimal(100)


def test_grid_floors_at_indexed_history(db, monkeypatch):
    sg = _setup(monkeypatch, 10**18)
    wanted = sg._wanted_buckets(NOW)
    floor = min(wanted)
    assert floor > NOW - 3 * 3600, "the grid must start after the earliest trade"
    assert max(wanted) == (NOW // 3600) * 3600 - 3600, "the open bucket is excluded"
