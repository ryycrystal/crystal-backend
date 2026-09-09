"""What one MON was worth at a point in the chain.

There were two answers to this: the live engine read prod's trades by block in five-minute buckets with a
one-MON minimum, and the replay read a seeded copy by timestamp in one-minute buckets with a minimum a
hundred times smaller, each with its own fallback. So the same flow could be valued differently depending on
which path produced it, and a fixture that must produce a specific number was not reproducible.

The rule lives here now. The only thing a caller supplies is where the trades are stored.
"""

from __future__ import annotations

from decimal import Decimal

from core.ledger.types import Rates

BUCKET_SECONDS = 300
MIN_SAMPLE_WEI = 10**18
CACHE_LIMIT = 8192

_RATE_FROM_TRADES = """
    SELECT usd_amount / (native_amount / 1e18)
    FROM launchpad_trades
    WHERE timestamp < %s AND native_amount >= %s AND usd_amount > 0
    ORDER BY timestamp DESC, block_number DESC, log_index DESC
    LIMIT 1
"""
SAMPLE_TABLE = "ledger_mon_usd_samples"
_RATE_FROM_SAMPLES = f"SELECT rate FROM {SAMPLE_TABLE} WHERE bucket_ts <= %s ORDER BY bucket_ts DESC LIMIT 1"
SEED_SAMPLES = f"""
    SELECT bucket, block_number, rate FROM (
        SELECT DISTINCT ON (timestamp / {BUCKET_SECONDS})
            timestamp / {BUCKET_SECONDS} * {BUCKET_SECONDS} AS bucket,
            block_number,
            usd_amount / (native_amount / 1e18) AS rate
        FROM launchpad_trades
        WHERE native_amount >= {MIN_SAMPLE_WEI} AND usd_amount > 0 AND timestamp > 0
        ORDER BY timestamp / {BUCKET_SECONDS}, timestamp DESC, block_number DESC, log_index DESC
    ) s
    ORDER BY bucket
"""

AUSD = "0x00000000efe302beaa2b3e6e1b18d08d69a9012a"
USDC = "0x754704bc059f8c67012fed69bc8a327a5aafb603"
AUSD_BAND = (Decimal("0.5"), Decimal("1.5"))
_AUSD_MARKET = """
    SELECT market, quote_decimals + scale_factor - base_decimals FROM crystal_markets
    WHERE lower(base_address) = %s AND lower(quote_address) = %s
    ORDER BY is_canonical DESC, updated_block DESC LIMIT 1
"""
_AUSD_RATE = """
    SELECT end_price FROM crystal_market_trades
    WHERE market = %s AND timestamp < %s AND end_price > 0
    ORDER BY timestamp DESC, block_number DESC, log_index DESC LIMIT 1
"""


def ausd_from_market(cur, at: int) -> Decimal | None:
    """AUSD in dollars from the last print on its USDC book before the bucket ends, or None.

    USDC is the dollar anchor and AUSD floats on its own book, so a dollar leg paid in AUSD is worth the
    book's price, not par. A print outside the plausible band is a thin-book tick rather than a depeg and
    is ignored, and no print at all means par: the fallback is one, never zero.
    """
    cur.execute(_AUSD_MARKET, (AUSD, USDC))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    market, factor = row[0], int(row[1] or 0)
    cur.execute(_AUSD_RATE, (market, at + BUCKET_SECONDS))
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    rate = Decimal(str(row[0])) / (Decimal(10) ** factor)
    return rate if AUSD_BAND[0] <= rate <= AUSD_BAND[1] else None


def bucket_start(ts) -> int:
    """The bucket a flow falls in, which is the instant both stores are asked about.

    A sample is the last qualifying trade inside its bucket, filed under the bucket's start. So the trades
    store has to be asked for the last qualifying trade before the bucket *ends*, or the two disagree by
    whatever traded in the same five minutes.
    """
    return (int(ts or 0) // BUCKET_SECONDS) * BUCKET_SECONDS


def meta_decimal(cur, key: str, default: Decimal) -> Decimal:
    cur.execute("SELECT value FROM launchpad_meta WHERE key = %s", (key,))
    row = cur.fetchone()
    if row and row[0] is not None:
        return Decimal(str(row[0]))
    return default


def from_trades(cur, at: int) -> Decimal | None:
    cur.execute(_RATE_FROM_TRADES, (at + BUCKET_SECONDS, MIN_SAMPLE_WEI))
    row = cur.fetchone()
    return Decimal(str(row[0])) if row and row[0] else None


def from_samples(cur, at: int) -> Decimal | None:
    cur.execute(_RATE_FROM_SAMPLES, (at,))
    row = cur.fetchone()
    return Decimal(str(row[0])) if row and row[0] is not None else None


class RateBook:
    """One bucket size, one minimum, one fallback, whichever store the samples come from."""

    def __init__(self, lookup=from_trades, ausd_lookup=ausd_from_market) -> None:
        self._lookup = lookup
        self._ausd_lookup = ausd_lookup
        self._cache: dict[int, Rates] = {}

    def __call__(self, blk: int, ts: int, cur) -> Rates:
        at = bucket_start(ts)
        cached = self._cache.get(at)
        if cached is not None:
            return cached
        mon_usd = self._lookup(cur, at)
        if mon_usd is None:
            mon_usd = meta_decimal(cur, "mon_price_usd", Decimal(0))
        rates = Rates(
            mon_usd=mon_usd,
            lvmon_rate=meta_decimal(cur, "lvmon_mon_rate", Decimal(1)),
            usdc_per_mon=mon_usd,
            ausd_usd=self._ausd_lookup(cur, at) or Decimal(1),
        )
        if len(self._cache) > CACHE_LIMIT:
            self._cache.clear()
        self._cache[at] = rates
        return rates
