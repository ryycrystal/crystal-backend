"""Until a token migrates its price is the curve's. A side pool can sit far off that, and letting its
end price set the mid drew a candle straight down to the pool and back up on the next curve trade.
WRAITH on 2026-09-15 was the case: a 0.000017 pool end price against a 0.00046 curve."""

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: E402
import state as state_mod  # noqa: E402
from core import token_decimals  # noqa: E402
from modules import nadfun as nf_events  # noqa: E402

TOKEN = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
POOL = "0x697be25fe455c09b1aa6fccba95a028bad57ba5c"
USER = "0x1234567890abcdef1234567890abcdef12345678"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

CURVE_MID = Decimal(5)
POOL_MID = Decimal(200) / Decimal(1000)
NOW = 1_789_000_000


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(token_decimals, "_rpc_decimals", lambda token: None)
    token_decimals.forget()
    token_decimals.prime({WMON: 18, TOKEN: 18})
    yield
    token_decimals.forget()


def _swap(monkeypatch, migrated, curve_idle_days=0.5):
    stub = MagicMock()
    stub.trade_exists.return_value = False
    stub.last_curve_trade_ts.return_value = 0 if curve_idle_days is None else int(NOW - curve_idle_days * 86400)
    monkeypatch.setattr(state_mod, "storage", stub)
    st = state_mod.State()
    st.mon_price_usd = Decimal("0.02")
    st.tokenToPrice = {WMON: Decimal("0.02")}
    st.v3_pools[POOL] = models.PoolInfo(pool=POOL, token_addr=TOKEN, native_addr=WMON, token_is_0=False)
    st.launchpad_tokens[TOKEN] = models.LaunchpadToken(
        token=TOKEN,
        creator=USER,
        name="T",
        symbol="T",
        metadata_cid="",
        description="",
        social1="",
        social2="",
        social3="",
        social4="",
        source=2,
        migrated=migrated,
        market=POOL,
        quote_token=WMON,
    )
    st.launchpad_tokens[TOKEN].last_price_native = CURVE_MID
    nf_events.parse_v2_pair_sync(POOL, [], f"{200 * 10**18:064x}" + f"{1000 * 10**18:064x}")
    st.apply_launchpad_trade(
        {"pool": POOL, "user": USER, "amount0": 21 * 10**18, "amount1": -(100 * 10**18), "sqrt_price_x96": 0},
        201,
        NOW,
        "0xwraith",
        0,
        POOL,
    )
    return st.launchpad_tokens[TOKEN], stub


def test_a_side_pool_does_not_move_an_unmigrated_tokens_price(monkeypatch):
    lp, _ = _swap(monkeypatch, migrated=False)
    assert lp.last_price_native == CURVE_MID, f"the curve mid must hold, got {lp.last_price_native}"


def test_the_trade_row_books_the_curve_mid_not_the_pool_end_price(monkeypatch):
    _, stub = _swap(monkeypatch, migrated=False)
    prices = [c.kwargs.get("price_native") for c in stub.insert_trade.call_args_list]
    assert prices, "the swap must still be recorded, it is real volume"
    assert all(Decimal(p) == CURVE_MID for p in prices), prices


def test_once_migrated_the_pool_is_the_price(monkeypatch):
    lp, _ = _swap(monkeypatch, migrated=True)
    assert lp.last_price_native == POOL_MID


def test_a_pool_takes_over_once_the_curve_has_been_abandoned(monkeypatch):
    # salmonad and tad: curves nowhere near complete but idle for two months while a pool
    # someone opened carries all the trading. holding a two month old curve price there
    # would freeze the chart, so the pool is the price
    lp, _ = _swap(monkeypatch, migrated=False, curve_idle_days=73)
    assert lp.last_price_native == POOL_MID


def test_a_token_that_never_traded_on_its_curve_is_priced_by_its_pool(monkeypatch):
    lp, _ = _swap(monkeypatch, migrated=False, curve_idle_days=None)
    assert lp.last_price_native == POOL_MID
