import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal

from api.api import _fmt, _fmt_usd


def test_fmt_artifacts_beyond_precision():
    assert _fmt(Decimal("1E-36")) == "0"
    assert _fmt(Decimal("1E-20")) == "0"


def test_fmt_at_threshold():
    assert _fmt(Decimal("1E-18")) == "0"
    assert _fmt(Decimal("-1E-18")) == "0"


def test_fmt_above_threshold():
    assert _fmt(Decimal("1E-17")) == "0.00000000000000001"
    assert _fmt(Decimal("0.000000000000001")) == "0.000000000000001"


def test_fmt_normal_values():
    assert _fmt(Decimal("123.456789")) == "123.456789"
    assert _fmt(Decimal("0.5")) == "0.5"
    assert _fmt(Decimal("-0.5")) == "-0.5"


def test_fmt_integers():
    assert _fmt(Decimal("1000000000000000000")) == "1000000000000000000"
    assert _fmt(Decimal("100")) == "100"
    assert _fmt(Decimal("1.000000000000000000")) == "1"


def test_fmt_edge_cases():
    assert _fmt(None) == "0"
    assert _fmt(Decimal("0")) == "0"


def test_fmt_usd_at_threshold():
    assert _fmt_usd(Decimal("0.000000001")) == "0"
    assert _fmt_usd(Decimal("0.00000001")) == "0"
    assert _fmt_usd(Decimal("-0.00000001")) == "0"


def test_fmt_usd_above_threshold():
    assert _fmt_usd(Decimal("0.00000002")) == "0.00000002"
    assert _fmt_usd(Decimal("0.12345678")) == "0.12345678"


def test_fmt_usd_quantization():
    assert _fmt_usd(Decimal("123.456789012")) == "123.45678901"


def test_fmt_usd_integers():
    assert _fmt_usd(Decimal("100.00000000")) == "100"


if __name__ == "__main__":
    test_fmt_artifacts_beyond_precision()
    test_fmt_at_threshold()
    test_fmt_above_threshold()
    test_fmt_normal_values()
    test_fmt_integers()
    test_fmt_edge_cases()
    test_fmt_usd_at_threshold()
    test_fmt_usd_above_threshold()
    test_fmt_usd_quantization()
    test_fmt_usd_integers()
    print("All tests passed!")
