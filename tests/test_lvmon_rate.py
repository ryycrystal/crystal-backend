"""lvmon is priced off its own pool rather than assumed equal to mon."""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import oracle  # noqa: E402

LIVE_SQRT_PRICE_X96 = 79723137456715038754121424902


def test_rate_matches_the_live_pool():
    rate = oracle.lvmon_rate_from_v3swap({"sqrt_price_x96": LIVE_SQRT_PRICE_X96})
    assert rate is not None
    assert Decimal("0.98") < rate < Decimal("0.99")
    assert abs(rate - Decimal("0.987621")) < Decimal("0.0001")


def test_missing_or_zero_price_is_ignored():
    for ev in ({}, {"sqrt_price_x96": 0}, {"sqrt_price_x96": None}, {"sqrt_price_x96": "nope"}):
        assert oracle.lvmon_rate_from_v3swap(ev) is None


def test_absurd_rates_are_rejected():
    q96 = Decimal(2) ** 96
    for target in (Decimal("0.2"), Decimal("2.0")):
        sqrt = int((Decimal(1) / target).sqrt() * q96)
        assert oracle.lvmon_rate_from_v3swap({"sqrt_price_x96": sqrt}) is None


def test_normal_discount_accepted():
    q96 = Decimal(2) ** 96
    sqrt = int((Decimal(1) / Decimal("0.95")).sqrt() * q96)
    rate = oracle.lvmon_rate_from_v3swap({"sqrt_price_x96": sqrt})
    assert rate is not None and abs(rate - Decimal("0.95")) < Decimal("0.001")
