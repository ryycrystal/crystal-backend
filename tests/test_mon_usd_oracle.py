from decimal import Decimal

from core.oracle import mon_price_from_v3swap

SQRT_PRICE_X96 = 12968519058578455003738
SPOT = Decimal("0.026793015895313073")


def _close(a, b, tol="0.0000001"):
    return abs(Decimal(a) - Decimal(b)) < Decimal(tol)


def test_spot_price_is_read_from_sqrt_price_not_swap_amounts():
    ev = {"amount0": -409800000000000000, "amount1": 11000, "sqrt_price_x96": SQRT_PRICE_X96}
    assert _close(mon_price_from_v3swap(ev), SPOT)


def test_swap_direction_does_not_move_the_rate():
    buy = {"amount0": 22418706800000000000000, "amount1": -599588800, "sqrt_price_x96": SQRT_PRICE_X96}
    sell = {"amount0": -409800000000000000, "amount1": 11000, "sqrt_price_x96": SQRT_PRICE_X96}
    assert mon_price_from_v3swap(buy) == mon_price_from_v3swap(sell)


def test_executed_ratio_alone_carries_the_fee_and_is_only_a_fallback():
    amounts_only = {"amount0": 22418706800000000000000, "amount1": -599588800}
    fallback = mon_price_from_v3swap(amounts_only)
    assert fallback is not None
    assert not _close(fallback, SPOT)
    assert abs(fallback / SPOT - 1) < Decimal("0.01")


def test_dust_swap_without_sqrt_price_is_refused():
    assert mon_price_from_v3swap({"amount0": 1000, "amount1": -1}) is None


def test_implausible_and_empty_swaps_are_refused():
    assert mon_price_from_v3swap({"amount0": 0, "amount1": 0, "sqrt_price_x96": 0}) is None
    assert mon_price_from_v3swap({"amount0": 1, "amount1": -1, "sqrt_price_x96": 1}) is None
