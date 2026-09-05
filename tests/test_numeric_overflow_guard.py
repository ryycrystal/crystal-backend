from decimal import Decimal

from core.sequencer import BatchAccumulator
from core.storage.launchpad import _fit_n50_18

TOKEN = "0x43cf5407bda1400498b8064d50a7e17528d87777"
USER = "0x71ee1cbac76d5d0d8d4702e77c4f880697fab8bd"
TXH = "0xf7a1a421356fbd60c4828e7b809566331f4d43c81a1f56c42f54d3141a7fd9c6"
TOKEN_AMT = 4457647997964394840429
NATIVE_AMT = 614922859069675369133

CAP = Decimal(10) ** 31
OVERFLOW_PRICE = Decimal(1) / ((Decimal(4295128740) / (Decimal(2) ** 96)) ** 2)


def test_fit_clamps_v4_min_sqrt_price_inverse():
    assert OVERFLOW_PRICE > Decimal(10) ** 32
    assert _fit_n50_18(OVERFLOW_PRICE) == CAP


def test_fit_clamps_negative_extreme():
    assert _fit_n50_18(-OVERFLOW_PRICE) == -CAP


def test_fit_zeros_non_finite():
    assert _fit_n50_18(Decimal("Infinity")) == Decimal(0)
    assert _fit_n50_18(Decimal("NaN")) == Decimal(0)
    assert _fit_n50_18(None) == Decimal(0)


def test_fit_preserves_normal_prices():
    for v in (Decimal("0.1379"), Decimal("0"), Decimal("15.55"), Decimal("1234567890.987654321")):
        assert _fit_n50_18(v) == v


def test_reconciled_trade_values_fit_untouched():
    price = Decimal(NATIVE_AMT) / Decimal(TOKEN_AMT)
    usd = (Decimal(NATIVE_AMT) / (Decimal(10) ** 18)) * Decimal("0.0253")
    assert _fit_n50_18(price) == price
    assert _fit_n50_18(usd) == usd


def test_insert_trades_batch_sanitizes_overflowing_row(monkeypatch):
    from core.storage import launchpad as L

    captured = {}

    class _Cur:
        pass

    def fake_execute_values(cur, sql, rows, page_size=None):
        captured["rows"] = list(rows)

    monkeypatch.setattr(L, "execute_values", fake_execute_values)

    good = (
        102263857,
        48,
        1788600000,
        TOKEN,
        USER,
        True,
        NATIVE_AMT,
        TOKEN_AMT,
        Decimal("15.55"),
        Decimal(NATIVE_AMT) / Decimal(TOKEN_AMT),
        TXH,
        0,
        0,
        0,
    )
    bad = (
        102263764,
        4,
        1788600000,
        "0x0158abff6d8344b35b5afffe7dbefc431b731a70",
        "0xffffffffffffffffffffffffffffffffff3f4087",
        False,
        78900012596557541714,
        361959467626928724,
        Decimal(0),
        OVERFLOW_PRICE,
        "0x2c0b356b4899948e86830eaf8a9d5f00b7da4c6b92422e47a59cc6c08ed1ca55",
        0,
        0,
        0,
    )
    L.insert_trades_batch([good, bad], _Cur())

    rows = captured["rows"]
    assert len(rows) == 2
    for r in rows:
        assert abs(Decimal(r[8])) <= CAP
        assert abs(Decimal(r[9])) <= CAP
    assert Decimal(rows[0][9]) == Decimal(NATIVE_AMT) / Decimal(TOKEN_AMT)
    assert Decimal(rows[1][9]) == CAP


def test_batch_accumulator_add_trade_preserves_values():
    b = BatchAccumulator()
    b.add_trade(
        block_number=102263857,
        log_index=48,
        timestamp=1788600000,
        token=TOKEN,
        user_address=USER,
        is_buy=True,
        native_amount=NATIVE_AMT,
        token_amount=TOKEN_AMT,
        usd_amount=Decimal("15.55"),
        price_native=Decimal(NATIVE_AMT) / Decimal(TOKEN_AMT),
        txhash=TXH,
    )
    assert len(b.trades) == 1
    row = b.trades[0]
    assert row[6] == NATIVE_AMT
    assert row[7] == TOKEN_AMT
    assert Decimal(row[9]) == Decimal(NATIVE_AMT) / Decimal(TOKEN_AMT)
