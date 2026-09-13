"""The order book routes refuse to serve only when the indexer is behind the chain, never because the
market happens to be quiet."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage as storage  # noqa: E402
from api.routes import orderbook as ob  # noqa: E402


def _arm(monkeypatch, *, head, indexed, processed_ago, last_trade_ago=3600):
    monkeypatch.setattr(ob, "STALE_SECONDS", 300.0)
    monkeypatch.setattr(ob, "STALE_BLOCKS", 300, raising=False)
    monkeypatch.setattr(ob, "_chain_head", lambda: head, raising=False)
    monkeypatch.setattr(storage, "indexer_head", lambda: (indexed, time.time() - processed_ago), raising=False)
    monkeypatch.setattr(storage, "latest_trade_timestamp", lambda: int(time.time() - last_trade_ago))


def test_a_quiet_market_with_a_current_indexer_is_not_stale(monkeypatch):
    _arm(monkeypatch, head=1_000_000, indexed=999_990, processed_ago=1, last_trade_ago=3600)
    assert ob.orderbook_data_is_stale() is False, "an hour without a trade is not an indexer problem"


def test_an_indexer_far_behind_the_chain_is_stale(monkeypatch):
    _arm(monkeypatch, head=1_000_000, indexed=999_000, processed_ago=1, last_trade_ago=5)
    assert ob.orderbook_data_is_stale() is True


def test_without_a_chain_head_the_age_of_the_last_processed_block_decides(monkeypatch):
    _arm(monkeypatch, head=None, indexed=999_990, processed_ago=10)
    assert ob.orderbook_data_is_stale() is False
    _arm(monkeypatch, head=None, indexed=999_990, processed_ago=3600)
    assert ob.orderbook_data_is_stale() is True


def test_no_processed_block_at_all_is_stale(monkeypatch):
    _arm(monkeypatch, head=1_000_000, indexed=None, processed_ago=0)
    monkeypatch.setattr(storage, "indexer_head", lambda: None, raising=False)
    assert ob.orderbook_data_is_stale() is True


def test_the_gate_can_be_switched_off(monkeypatch):
    _arm(monkeypatch, head=1_000_000, indexed=1, processed_ago=10**6)
    monkeypatch.setattr(ob, "STALE_SECONDS", 0.0)
    assert ob.orderbook_data_is_stale() is False
