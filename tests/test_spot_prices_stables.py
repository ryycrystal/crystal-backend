from decimal import Decimal

import api.api
from api.spot_data import NATIVE, WMON, spot_prices_from_markets

USDC = api.api.USDC
AUSD = api.api.AUSD
XAUT = "0x01bff41798a0bcf287b996046ca68b395dbc1071"
CHIP = "0xc0ffee0000000000000000000000000000000001"
FEED = Decimal("0.0257")


def test_a_stable_quote_stays_at_one_dollar_no_matter_what_the_wmon_book_says():
    prices = spot_prices_from_markets([(WMON, USDC, Decimal("0.026724"))], FEED)
    assert prices[USDC] == Decimal(1)
    assert prices[AUSD] == Decimal(1)
    assert prices[WMON] == FEED
    assert prices[NATIVE] == FEED


def test_a_stable_priced_as_base_of_a_native_market_is_still_one_dollar():
    prices = spot_prices_from_markets([(USDC, WMON, Decimal("37.4"))], FEED)
    assert prices[USDC] == Decimal(1)


def test_tokens_quoted_in_a_stable_take_the_market_price_as_dollars():
    prices = spot_prices_from_markets([(XAUT, USDC, Decimal("3412.5"))], FEED)
    assert prices[XAUT] == Decimal("3412.5")


def test_tokens_quoted_in_wmon_are_priced_through_the_feed():
    prices = spot_prices_from_markets([(CHIP, WMON, Decimal("0.01"))], FEED)
    assert prices[CHIP] == Decimal("0.01") * FEED


def test_a_non_stable_quote_of_a_wmon_market_is_still_derived_by_inversion():
    other = "0x0000000000000000000000000000000000000abc"
    prices = spot_prices_from_markets([(WMON, other, Decimal("4"))], FEED)
    assert prices[other] == FEED / Decimal(4)


def test_without_a_feed_stables_are_still_one_dollar_and_native_is_unknown():
    prices = spot_prices_from_markets([(WMON, USDC, Decimal("0.026724"))], Decimal(0))
    assert prices[USDC] == Decimal(1)
    assert prices[WMON] is None


def test_ausd_takes_the_oracle_rate_while_usdc_stays_pegged(monkeypatch):
    monkeypatch.setattr(api.api, "_ausd_price_usd", lambda: Decimal("0.99"))
    prices = spot_prices_from_markets([(XAUT, AUSD, Decimal("100"))], FEED)
    assert prices[USDC] == Decimal(1)
    assert prices[AUSD] == Decimal("0.99")
    assert prices[XAUT] == Decimal("99")


def test_stable_quote_usd_only_floats_ausd(monkeypatch):
    monkeypatch.setattr(api.api, "_ausd_price_usd", lambda: Decimal("0.97"))
    assert api.api.stable_quote_usd(USDC) == Decimal(1)
    assert api.api.stable_quote_usd(AUSD) == Decimal("0.97")
    assert api.api._quote_price_usd(AUSD) == Decimal("0.97")
    assert api.api._quote_price_usd(USDC) == Decimal(1)
