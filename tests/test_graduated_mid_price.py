from decimal import Decimal

import state as _st


class _Market:
    def __init__(self, reserve_quote, reserve_base, price, base_decimals=18, quote_decimals=18):
        self.reserveQuote = reserve_quote
        self.reserveBase = reserve_base
        self.price = price
        self.baseDecimals = base_decimals
        self.quoteDecimals = quote_decimals


def _mid(mi):
    return _st.State._graduated_mid_price_locked(None, mi)


def test_mid_comes_from_reserves_not_the_fill_price():
    mi = _Market(
        reserve_quote=673531800000000000000,
        reserve_base=983833926192600000000000000,
        price=Decimal("6.92e-7"),
    )
    assert abs(_mid(mi) - Decimal("6.8460e-7")) < Decimal("1e-11")


def test_buy_and_sell_at_the_same_reserves_price_identically():
    reserves = (662750530282049235097, 999999999992615479178855752)
    after_buy = _Market(*reserves, price=Decimal("6.92e-7"))
    after_sell = _Market(*reserves, price=Decimal("6.56e-7"))
    assert _mid(after_buy) == _mid(after_sell)


def test_fill_price_carries_the_fee_but_the_mid_does_not():
    mi = _Market(
        reserve_quote=662750530282049235097,
        reserve_base=999999999992615479178855752,
        price=Decimal("6.56e-7"),
    )
    mid = _mid(mi)
    assert mid > mi.price
    assert abs(mi.price / mid - 1) < Decimal("0.02")


def test_decimal_mismatch_is_scaled_out():
    mi = _Market(reserve_quote=1000 * 10**6, reserve_base=1000 * 10**18, price=0, quote_decimals=6)
    assert _mid(mi) == Decimal(1)


def test_missing_or_zero_reserves_fall_back():
    assert _mid(_Market(0, 0, price=1)) is None
    assert _mid(_Market(1, 0, price=1)) is None
