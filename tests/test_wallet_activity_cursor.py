"""Load-more on the activity feed 500ed past the first page: the keyset cut named timestamp,
log_index and txhash on referral_bindings and launchpad_tokens, which have none of those. Those
three rows are synthesized, so the cut has to compare the expressions their select emits."""

import os
import re
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.storage import launchpad as lp_storage  # noqa: E402

WALLET = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"


def _sql(monkeypatch, **kwargs) -> str:
    captured = {}

    class Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql

        def fetchall(self):
            return []

    @contextmanager
    def fake_cursor(*_a, **_k):
        yield Cur()

    monkeypatch.setattr(lp_storage, "db_cursor", fake_cursor)
    lp_storage.wallet_activity([WALLET], limit=50, **kwargs)
    return captured["sql"]


def _branch(sql: str, label: str) -> str:
    start = sql.index(f"'{label}'")
    nxt = sql.find("UNION ALL", start)
    return sql[start : nxt if nxt != -1 else len(sql)]


def test_a_cursor_never_names_columns_the_synthesized_tables_lack(monkeypatch):
    sql = _sql(monkeypatch, before_key=(1788554381, 101997572, 42, "0xfa65"))
    assert not re.search(r"\br\.txhash\b", sql)
    for bad in ("k.timestamp", "k.block_number", "k.log_index", "k.txhash"):
        assert bad not in sql, bad


def test_each_synthesized_row_pages_on_its_own_time_column(monkeypatch):
    sql = _sql(monkeypatch, before_key=(1788554381, 101997572, 42, "0xfa65"))
    assert "k.created_at" in _branch(sql, "token_create")
    assert "k.migrated_at" in _branch(sql, "token_graduate")
    assert "CONCAT('referral-'" in _branch(sql, "referral_use")


def test_a_plain_timestamp_cut_uses_the_real_columns_too(monkeypatch):
    sql = _sql(monkeypatch, before_ts=1788554381)
    assert "k.timestamp" not in sql
    assert "k.created_at < " in _branch(sql, "token_create")


def test_the_first_page_adds_no_cut(monkeypatch):
    sql = _sql(monkeypatch)
    assert "%(cts)s" not in sql and "%(cut)s" not in sql
