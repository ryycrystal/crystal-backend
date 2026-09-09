"""The replay and the live engine must value the same flow identically (feedback4 finding 12b)."""

from decimal import Decimal

from core.ledger.rates import BUCKET_SECONDS, MIN_SAMPLE_WEI, RateBook, bucket_start, from_samples, from_trades

TRADES = [
    (1_700_000_000, 100, 2 * 10**18, Decimal("60")),
    (1_700_000_100, 101, 10**15, Decimal("999")),
    (1_700_000_400, 102, 5 * 10**18, Decimal("150")),
]


class FakeCursor:
    """Answers both rate queries from one set of trades, the way the two real stores would."""

    def __init__(self, trades, meta=None):
        self.trades = trades
        self.meta = meta or {}
        self._result = None

    def execute(self, sql, params=()):
        if "launchpad_meta" in sql:
            value = self.meta.get(params[0])
            self._result = (value,) if value is not None else None
        elif "crystal_market" in sql:
            self._result = None
        elif "launchpad_trades" in sql:
            before, minimum = params
            usable = [t for t in self.trades if t[0] < before and t[2] >= minimum]
            self._result = (usable[-1][3] / (Decimal(usable[-1][2]) / Decimal(10**18)),) if usable else None
        else:
            at = params[0]
            buckets = {}
            for ts, _, native, usd in self.trades:
                if native >= MIN_SAMPLE_WEI:
                    buckets[bucket_start(ts)] = usd / (Decimal(native) / Decimal(10**18))
            earlier = [b for b in sorted(buckets) if b <= at]
            self._result = (buckets[earlier[-1]],) if earlier else None

    def fetchone(self):
        return self._result


def test_both_stores_answer_with_the_same_rate_for_the_same_flow():
    meta = {"mon_price_usd": "0.03", "lvmon_mon_rate": "1"}
    for ts in (1_700_000_000, 1_700_000_150, 1_700_000_299, 1_700_000_400, 1_700_000_900):
        live = RateBook(from_trades)(0, ts, FakeCursor(TRADES, meta))
        replayed = RateBook(from_samples)(0, ts, FakeCursor(TRADES, meta))
        assert live == replayed, ts


def test_a_bucket_is_the_same_length_and_edge_for_both():
    assert BUCKET_SECONDS == 300
    assert bucket_start(1_699_999_800) == 1_699_999_800
    assert bucket_start(1_700_000_099) == 1_699_999_800
    assert bucket_start(1_700_000_100) == 1_700_000_100


def test_a_trade_too_small_to_price_is_ignored_by_both():
    small = [(1_700_000_000, 100, 10**15, Decimal("999"))]
    meta = {"mon_price_usd": "0.03", "lvmon_mon_rate": "1"}
    live = RateBook(from_trades)(0, 1_700_000_000, FakeCursor(small, meta))
    replayed = RateBook(from_samples)(0, 1_700_000_000, FakeCursor(small, meta))
    assert live.mon_usd == replayed.mon_usd == Decimal("0.03"), "both fall back to the recorded rate"


def test_the_rate_book_caches_per_bucket_not_per_call():
    calls = []

    def counting(cur, at):
        calls.append(at)
        return Decimal(7)

    book = RateBook(counting)
    cur = FakeCursor(TRADES, {"lvmon_mon_rate": "1"})
    book(0, 1_699_999_800, cur)
    book(0, 1_700_000_099, cur)
    assert calls == [1_699_999_800]
    book(0, 1_700_000_100, cur)
    assert calls == [1_699_999_800, 1_700_000_100]


class AusdCursor(FakeCursor):
    """Adds the AUSD/USDC book to the fake: the market row and its trades, priced in raw ticks."""

    def __init__(self, trades, meta=None, book=(), factor=4):
        super().__init__(trades, meta)
        self.book = list(book)
        self.factor = factor

    def execute(self, sql, params=()):
        if "crystal_market_trades" in sql:
            before = params[1]
            usable = [t for t in self.book if t[0] < before]
            self._result = (usable[-1][1],) if usable else None
        elif "crystal_markets" in sql:
            self._result = ("0xausdmarket", self.factor)
        else:
            super().execute(sql, params)


def test_ausd_is_priced_off_its_own_book_and_falls_back_to_par():
    from core.ledger.rates import ausd_from_market

    meta = {"mon_price_usd": Decimal("0.03")}
    book = [(1_699_999_000, 9_800), (1_700_000_050, 9_900)]
    rates = RateBook(from_trades, ausd_from_market)(0, 1_700_000_000, AusdCursor(TRADES, meta, book))
    assert rates.ausd_usd == Decimal("0.99"), "the last print before the bucket ends, scaled by the market's factor"
    rates = RateBook(from_trades, ausd_from_market)(0, 1_699_998_000, AusdCursor(TRADES, meta, book))
    assert rates.ausd_usd == Decimal(1), "no print yet means par, not zero"
    rates = RateBook(from_trades, ausd_from_market)(
        0, 1_700_000_000, AusdCursor(TRADES, meta, [(1_699_999_000, 30_000)])
    )
    assert rates.ausd_usd == Decimal(1), "a print outside the plausible band is a thin-book tick, not a depeg"


def test_the_rate_book_carries_the_registry_units_and_defaults_to_the_known_quotes():
    from core.ledger.types import QUOTE_DECIMALS, USDC

    meta = {"mon_price_usd": Decimal("0.03")}
    plain = RateBook(from_trades)(0, 1_700_000_000, FakeCursor(TRADES, meta))
    assert plain.units == QUOTE_DECIMALS
    extra = "0x" + "cb" * 20
    book = RateBook(from_trades, units={extra: 8})(0, 1_700_000_000, FakeCursor(TRADES, meta))
    assert book.units[extra] == 8 and book.units[USDC] == 6
