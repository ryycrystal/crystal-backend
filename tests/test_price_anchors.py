import threading
from decimal import Decimal

import models
import state as state_mod
from state import AUSD, LVMON, USDC, WMON, State

CHIP = "0xc0ffee0000000000000000000000000000000001"
USDT = "0xc0ffee0000000000000000000000000000000002"
XAUT = "0x01bff41798a0bcf287b996046ca68b395dbc1071"
ORACLE = Decimal("0.0257")
BOOK = Decimal("0.0268")


def _market(base, quote, price, base_ticker="", base_name=""):
    mi = models.MarketInfo.__new__(models.MarketInfo)
    mi.market = f"0xm{base[-6:]}{quote[-6:]}"
    mi.baseAddress, mi.quoteAddress = base, quote
    mi.baseTicker, mi.baseName = base_ticker, base_name
    mi.price = Decimal(price)
    return mi


def _state(markets, ausd=Decimal(1)):
    st = State.__new__(State)
    st._lock = threading.RLock()
    st.mon_price_usd = ORACLE
    st.lvmon_rate = Decimal("1.05")
    st.ausd_price_usd = Decimal(ausd)
    st.tokenToPrice = {}
    st.tokenGraph = {}
    st._seed_aux_prices_locked()
    for mi in markets:
        st._add_market_to_token_graph_locked(mi)
    return st


def test_the_wmon_usdc_book_never_moves_wmon_off_the_oracle():
    st = _state([_market(WMON, USDC, BOOK)])
    st.sweep()
    assert st.tokenToPrice[WMON] == ORACLE
    assert st.tokenToPrice[USDC] == Decimal(1)
    assert st.tokenToPrice[AUSD] == Decimal(1)
    assert st.tokenToPrice[LVMON] == ORACLE * Decimal("1.05")


def test_wmon_quoted_tokens_are_priced_off_the_oracle_not_the_book():
    st = _state([_market(WMON, USDC, BOOK), _market(CHIP, WMON, "0.01")])
    st.sweep()
    assert st.tokenToPrice[CHIP] == Decimal("0.01") * ORACLE


def test_a_stable_recognised_by_ticker_is_not_repriced_by_its_own_market():
    st = _state([_market(USDT, WMON, "37.5", base_ticker="USDT", base_name="Tether USD")])
    st._maybe_seed_stable_price_locked(USDT, "USDT", "Tether USD")
    st.sweep()
    assert st.tokenToPrice[USDT] == Decimal(1)


def test_a_stable_as_base_of_a_native_market_stays_at_one_dollar():
    st = _state([_market(USDC, WMON, "38.9")])
    st.sweep()
    assert st.tokenToPrice[USDC] == Decimal(1)


def test_pinned_set_covers_the_anchors():
    assert {USDC, AUSD, WMON, LVMON} <= set(state_mod.PINNED_PRICE_TOKENS)


def test_only_usdc_is_hard_pegged_to_a_dollar():
    assert state_mod.USD_PEGGED_TOKENS == (USDC,)
    assert AUSD not in state_mod.USD_PEGGED_TOKENS


def test_ausd_seeds_from_its_own_rate_rather_than_a_hardcoded_dollar():
    st = _state([], ausd=Decimal("0.9971"))
    assert st.tokenToPrice[AUSD] == Decimal("0.9971")
    assert st.tokenToPrice[USDC] == Decimal(1)


def test_the_ausd_usdc_book_moves_the_ausd_price():
    st = _state([])
    st.set_ausd_price_usd(Decimal("0.994"))
    assert st.ausd_price_usd == Decimal("0.994")
    assert st.tokenToPrice[AUSD] == Decimal("0.994")


def test_an_implausible_ausd_print_is_ignored_and_the_last_rate_stands():
    st = _state([], ausd=Decimal("0.998"))
    for bogus in (Decimal(0), Decimal("0.2"), Decimal("4"), None, "not a number"):
        st.set_ausd_price_usd(bogus)
        assert st.ausd_price_usd == Decimal("0.998")
        assert st.tokenToPrice[AUSD] == Decimal("0.998")


def test_a_token_quoted_in_ausd_is_priced_through_the_ausd_rate():
    st = _state([_market(CHIP, AUSD, "2")], ausd=Decimal("0.5"))
    st.sweep()
    assert st.tokenToPrice[CHIP] == Decimal(1)


class _FakeCursor:
    def __init__(self, rows_by_quote):
        self.rows_by_quote = rows_by_quote
        self._rows = []

    def execute(self, sql, params):
        self._rows = list(self.rows_by_quote.get(params[1], []))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_graph_db(monkeypatch, rows_by_quote):
    import api.spot_graph as sg

    monkeypatch.setattr(sg, "db_cursor", lambda: _FakeCursor(rows_by_quote))
    return sg


def test_graph_prices_both_stables_at_one_dollar_by_address(monkeypatch):
    sg = _patch_graph_db(monkeypatch, {})
    assert sg._token_price_at({"address": USDC, "ticker": "USDC"}, 1000, ORACLE, WMON) == Decimal(1)
    assert sg._token_price_at({"address": AUSD, "ticker": "AUSD"}, 1000, ORACLE, WMON) == Decimal(1)
    assert sg._token_price_at({"address": AUSD, "ticker": ""}, 1000, ORACLE, WMON) == Decimal(1)


def test_graph_prices_a_usdc_quoted_token_from_its_own_syncs(monkeypatch):
    rows = {USDC: [(3_400 * 10**6, 1 * 10**6, 6, 6)]}
    sg = _patch_graph_db(monkeypatch, rows)
    assert sg._token_price_at({"address": XAUT, "ticker": "XAUt0"}, 1000, ORACLE, WMON) == Decimal(3400)


def test_graph_prefers_the_wmon_market_and_prices_it_through_the_oracle(monkeypatch):
    rows = {
        WMON: [
            (2 * 10**18, 100 * 10**18, 18, 18),
            (4 * 10**18, 100 * 10**18, 18, 18),
            (3 * 10**18, 100 * 10**18, 18, 18),
        ]
    }
    sg = _patch_graph_db(monkeypatch, rows)
    price = sg._token_price_at({"address": CHIP, "ticker": "CHIP"}, 1000, ORACLE, WMON)
    assert price == Decimal("0.03") * ORACLE


def test_graph_returns_nothing_for_a_token_with_no_syncs_anywhere(monkeypatch):
    sg = _patch_graph_db(monkeypatch, {})
    assert sg._token_price_at({"address": CHIP, "ticker": "CHIP"}, 1000, ORACLE, WMON) is None
