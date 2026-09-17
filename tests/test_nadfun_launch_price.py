import os
import sys
import threading
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import core.chain as h
import models
import state as st
from core.adapters import nadfun as nadfun_geo

TOKEN = "0x" + "e" * 40
WAD = 10**18
V1_TOKENS = 1_073_000_191


class _Persisted(Exception):
    pass


def _create(monkeypatch, capsys, **reserves):
    s = object.__new__(st.State)
    s._lock = threading.RLock()
    s.launchpad_tokens = {}
    written = {}

    def upsert(**kwargs):
        written.update(kwargs)
        raise _Persisted

    monkeypatch.setattr(st, "_source_for_emitter", lambda _a: nadfun_geo.SOURCE_V1)
    monkeypatch.setattr(st.storage, "upsert_token_created", upsert)
    ev = {"token": TOKEN, "creator": "0x" + "d" * 40, "name": "T", "symbol": "T", "quote_token": st.WMON, **reserves}
    with pytest.raises(_Persisted):
        st.State.apply_token_created(s, 1, ev, 1, h.NADFUN_ADDR, cur=None)
    return written["last_price_native"], capsys.readouterr().out


@pytest.mark.parametrize("virtual_mon", [90_000, 225_000, 180_000])
def test_every_nadfun_setting_prices_from_its_own_create_event(monkeypatch, capsys, virtual_mon):
    price, out = _create(monkeypatch, capsys, native_reserve=virtual_mon * WAD, token_reserve=V1_TOKENS * WAD)
    assert price == Decimal(virtual_mon) / Decimal(V1_TOKENS)
    assert "carried no curve reserves" not in out


def test_a_create_without_reserves_is_not_given_a_guessed_price(monkeypatch, capsys):
    price, out = _create(monkeypatch, capsys)
    assert price == models.LaunchpadToken.__dataclass_fields__["last_price_native"].default
    assert price != Decimal(90_000) / Decimal(V1_TOKENS)
    assert "carried no curve reserves" in out
