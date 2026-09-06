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


class _CapturingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, tuple(params) if params else ()))

    def executemany(self, sql, seq):
        for p in seq:
            self.calls.append(("execute", sql, tuple(p)))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _flatten(x):
    if isinstance(x, (list, tuple)):
        for e in x:
            yield from _flatten(e)
    else:
        yield x


def _numeric_params(calls):
    for _, _sql, params in calls:
        for v in _flatten(params):
            if isinstance(v, (int, Decimal)) and not isinstance(v, bool):
                yield v
            elif isinstance(v, float):
                yield Decimal(str(v))


def test_full_batch_flush_never_writes_a_numeric_over_cap(monkeypatch):
    from core.storage import launchpad as L

    cur = _CapturingCursor()

    def fake_execute_values(cur_arg, sql, rows, page_size=None, template=None):
        for row in rows:
            cur.calls.append(("execute_values", sql, tuple(row)))

    monkeypatch.setattr(L, "execute_values", fake_execute_values)

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

    b.add_trade(
        block_number=102263764,
        log_index=4,
        timestamp=1788600000,
        token="0x0158abff6d8344b35b5afffe7dbefc431b731a70",
        user_address="0xffffffffffffffffffffffffffffffffff3f4087",
        is_buy=False,
        native_amount=78900012596557541714,
        token_amount=361959467626928724,
        usd_amount=OVERFLOW_PRICE * Decimal("0.0253"),
        price_native=OVERFLOW_PRICE,
        txhash="0x2c0b356b4899948e86830eaf8a9d5f00b7da4c6b92422e47a59cc6c08ed1ca55",
    )

    b.set_token_state(
        "0x0158abff6d8344b35b5afffe7dbefc431b731a70",
        {
            "last_price_native": OVERFLOW_PRICE,
            "native_volume": 10**23,
            "token_volume": 10**23,
            "volume_usd": OVERFLOW_PRICE,
            "fees_usd": OVERFLOW_PRICE,
            "buy_count": 1,
            "sell_count": 1,
            "tx_count": 2,
            "circulating_supply": 10**26,
            "approaching_75": False,
            "approaching_75_block": None,
            "approaching_75_at": None,
            "snipers_count": 0,
            "curve_native_reserve": 0,
            "curve_token_reserve": 0,
        },
    )

    b.add_user_delta(USER, NATIVE_AMT, OVERFLOW_PRICE, trade_count_delta=1)

    b.add_position_delta(
        user_address=USER,
        token="0x0158abff6d8344b35b5afffe7dbefc431b731a70",
        token_bought_delta=0,
        token_sold_delta=361959467626928724,
        native_spent_delta=0,
        native_received_delta=78900012596557541714,
        balance_token_delta=-361959467626928724,
        realized_pnl_delta=OVERFLOW_PRICE,
        trade_count_delta=1,
        buy_count_delta=0,
        sell_count_delta=1,
        last_price_native=OVERFLOW_PRICE,
        cost_basis_delta=-100,
    )

    b.add_ohlcv(
        "0x0158abff6d8344b35b5afffe7dbefc431b731a70",
        60,
        1788600000,
        OVERFLOW_PRICE,
        78900012596557541714,
        OVERFLOW_PRICE,
    )

    b.add_sniper(TOKEN, USER)

    b.flush(cur)

    assert cur.calls, "batch.flush produced no SQL"

    offenders = []
    for v in _numeric_params(cur.calls):
        try:
            d = Decimal(v)
        except Exception:
            continue
        if not d.is_finite() or abs(d) > CAP:
            offenders.append(v)

    assert not offenders, f"batch.flush() sent {len(offenders)} numeric params exceeding {CAP}. First: {offenders[:3]}"

    for _, sql, _params in cur.calls:
        if "crystal_unrealized_pnl" in sql:
            assert "LEAST(GREATEST(" in sql, (
                "crystal_unrealized_pnl result must be wrapped in LEAST(GREATEST(...)) "
                "so its output cannot overflow numeric(50,18):\n" + sql
            )


def test_clamp_warning_fires_only_when_actually_clamped(capsys):
    _fit_n50_18(Decimal("1"))
    _fit_n50_18(Decimal(NATIVE_AMT) / Decimal(TOKEN_AMT))
    out = capsys.readouterr().out
    assert "[clamp]" not in out

    _fit_n50_18(OVERFLOW_PRICE)
    out = capsys.readouterr().out
    assert "[clamp]" in out

    _fit_n50_18(Decimal("Infinity"))
    out = capsys.readouterr().out
    assert "[clamp]" in out
