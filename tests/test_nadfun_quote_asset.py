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


def _trade_state(quote):
    s = object.__new__(st.State)
    s._lock = __import__("threading").RLock()
    lp = __import__("models").LaunchpadToken(
        token=TOKEN,
        creator="",
        name="T",
        symbol="T",
        metadata_cid="",
        description="",
        social1="",
        social2="",
        social3="",
        social4="",
    )
    lp.source = nadfun_geo.SOURCE_V2
    lp.quote_token = quote
    s.launchpad_tokens = {TOKEN: lp}
    s._basis_reset_if_new_block = lambda *a, **k: None
    return s


def _trade_event():
    return {"token": TOKEN, "user": "0x" + "e" * 40, "is_buy": True, "amount_in": 10**18, "amount_out": 10**21}


def _run_trade(s):
    try:
        st.State.apply_launchpad_trade(s, _trade_event(), 1, 1, "0x" + "f" * 64, 0, h.NADFUN_V2_ADDR, cur=None)
    except RuntimeError:
        # past the guard the real path wants a db; the price write right after the
        # guard has already happened by then, so it is what these tests read
        pass
    return s.launchpad_tokens[TOKEN].last_price_native


def test_a_trade_on_an_already_loaded_non_mon_token_writes_nothing(monkeypatch):
    monkeypatch.setattr(st.storage, "trade_exists", lambda *a, **k: False)
    s = _trade_state(LVMON)
    s.launchpad_tokens[TOKEN].last_price_native = __import__("decimal").Decimal(0)
    assert _run_trade(s) == 0


def test_a_trade_on_a_mon_token_proceeds_past_the_guard(monkeypatch):
    monkeypatch.setattr(st.storage, "trade_exists", lambda *a, **k: False)
    s = _trade_state(st.WMON)
    s.launchpad_tokens[TOKEN].last_price_native = __import__("decimal").Decimal(0)
    assert _run_trade(s) > 0


def test_a_v1_token_graduated_into_a_stable_pool_keeps_pricing(monkeypatch):
    # v1 always launches in mon, so a usdc quote_token on it came from the pool it
    # graduated into. it must keep pricing rather than be mistaken for a non-mon launch
    monkeypatch.setattr(st.storage, "trade_exists", lambda *a, **k: False)
    s = _trade_state(USDC)
    s.launchpad_tokens[TOKEN].source = nadfun_geo.SOURCE_V1
    s.launchpad_tokens[TOKEN].migrated = True
    s.launchpad_tokens[TOKEN].last_price_native = __import__("decimal").Decimal(0)
    assert _run_trade(s) > 0


def test_a_migrated_v2_token_that_launched_off_mon_is_still_skipped(monkeypatch):
    # the busy case: a v2 curve launched in lvmon and graduated. its quote_token is its
    # launch quote, so migrating must not let its trades back in
    monkeypatch.setattr(st.storage, "trade_exists", lambda *a, **k: False)
    s = _trade_state(LVMON)
    s.launchpad_tokens[TOKEN].migrated = True
    s.launchpad_tokens[TOKEN].last_price_native = __import__("decimal").Decimal(0)
    assert _run_trade(s) == 0
