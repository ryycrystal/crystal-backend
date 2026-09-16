import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.chain as h
import state as st
from core.adapters import nadfun as nadfun_geo

TOKEN = "0x" + "b" * 40
USDC = "0x" + "c" * 40
LVMON = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"


def _event(quote):
    return {
        "token": TOKEN,
        "creator": "0x" + "d" * 40,
        "name": "Test",
        "symbol": "TEST",
        "quote_token": quote,
    }


def _apply(monkeypatch, quote):
    s = object.__new__(st.State)
    s._lock = __import__("threading").RLock()
    s.launchpad_tokens = {}
    marked = []
    monkeypatch.setattr(st.storage, "mark_nadfun_v2", lambda t, cur=None: marked.append(t))
    monkeypatch.setattr(st, "_source_for_emitter", lambda _a: nadfun_geo.SOURCE_V2)
    try:
        st.State.apply_token_created(s, 1, _event(quote), 1, h.NADFUN_V2_ADDR, cur=None)
    except RuntimeError:
        # past the guard the real write path wants a db; reaching it is the signal
        # this test cares about, so only the guard itself is under test here
        pass
    return marked, s.launchpad_tokens


def test_a_non_mon_quote_is_skipped_before_anything_is_written(monkeypatch):
    marked, tokens = _apply(monkeypatch, USDC)
    assert marked == []
    assert tokens == {}


def test_a_mon_quoted_curve_is_still_indexed(monkeypatch):
    marked, _ = _apply(monkeypatch, st.WMON)
    assert marked == [TOKEN]


def test_lvmon_is_skipped_too_only_mon_is_accepted(monkeypatch):
    marked, tokens = _apply(monkeypatch, LVMON)
    assert marked == []
    assert tokens == {}


def test_a_missing_quote_defaults_to_mon_and_is_indexed(monkeypatch):
    marked, _ = _apply(monkeypatch, "")
    assert marked == [TOKEN]


def test_discovery_refuses_a_non_mon_quote(monkeypatch):
    s = object.__new__(st.State)
    s._lock = __import__("threading").RLock()
    s.launchpad_tokens = {}
    monkeypatch.setattr(st, "_fetch_token_string", lambda *a, **k: "x")
    monkeypatch.setattr(st, "_fetch_v2_quote_token", lambda _t: USDC)
    assert st.State.ensure_v2_launchpad_token(s, TOKEN, 1, 1) is False
    assert s.launchpad_tokens == {}
