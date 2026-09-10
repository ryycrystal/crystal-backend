"""Decimals are looked up, not assumed, and a pool's price is its mid rather than a taker's fill.

The defect these cover cost nine months of broken charts: a 6-decimal quote read as 18 booked
13.21 USDC as 0.0000000000132 MON and priced the pool 10^12 low.
"""

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
USDC = "0x754704bc059f8c67012fed69bc8a327a5aafb603"
UNKNOWN = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test here may reach the chain: a miss must be a miss, not a slow success."""
    monkeypatch.setattr(token_decimals, "_rpc_decimals", lambda token: None)
    token_decimals.forget()
    yield
    token_decimals.forget()


def test_a_known_token_comes_from_the_table_and_is_asked_for_only_once():
    store = MagicMock()
    store.get_token_decimals.return_value = 6
    assert token_decimals.decimals_for(USDC, storage_module=store) == 6
    assert token_decimals.decimals_for(USDC, storage_module=store) == 6
    assert store.get_token_decimals.call_count == 1, "the second call must come from memory"


def test_a_chain_read_is_written_back_so_the_next_process_does_not_repeat_it(monkeypatch):
    monkeypatch.setattr(token_decimals, "_rpc_decimals", lambda token: 6)
    store = MagicMock()
    store.get_token_decimals.return_value = None
    assert token_decimals.decimals_for(USDC, storage_module=store) == 6
    store.upsert_token_decimals.assert_called_once_with(USDC, 6, cur=None)


def test_an_unknown_token_is_none_rather_than_a_confident_eighteen():
    store = MagicMock()
    store.get_token_decimals.return_value = None
    assert token_decimals.decimals_for(UNKNOWN, storage_module=store) is None
    assert token_decimals.decimals_for("not-an-address", storage_module=store) is None


def test_a_nonsense_stored_value_is_refused():
    store = MagicMock()
    store.get_token_decimals.return_value = 99
    assert token_decimals.decimals_for(USDC, storage_module=store) is None


def _state(monkeypatch, quote: str, token_is_0: bool = False):
    stub = MagicMock()
    stub.trade_exists.return_value = False
    monkeypatch.setattr(state_mod, "storage", stub)
    st = state_mod.State()
    st.mon_price_usd = Decimal("0.02")
    st.tokenToPrice = {WMON: Decimal("0.02"), USDC: Decimal(1)}
    st.v3_pools[POOL] = models.PoolInfo(pool=POOL, token_addr=TOKEN, native_addr=quote, token_is_0=token_is_0)
    # a pool swap on a token the indexer never registered is dropped before pricing, so the
    # token has to exist for these to exercise the price at all
    st.launchpad_tokens[TOKEN] = models.LaunchpadToken(
        token=TOKEN,
        creator=USER,
        name="Test",
        symbol="TEST",
        metadata_cid="",
        description="",
        social1="",
        social2="",
        social3="",
        social4="",
        source=1,
        migrated=True,
        market=POOL,
        quote_token=quote,
    )
    return st


def test_a_six_decimal_quote_scales_the_pool_ratio_instead_of_reading_it_raw(monkeypatch):
    """A pool holding 200 USDC against 1000 tokens is 0.2 USDC per token, which at 0.02 USD per
    MON is 10 MON per token. Read raw, the same reserves give 2e-13."""
    token_decimals.prime({USDC: 6, TOKEN: 18})
    st = _state(monkeypatch, USDC)
    nf_events.parse_v2_pair_sync(POOL, [], f"{200 * 10**6:064x}" + f"{1000 * 10**18:064x}")

    st.apply_launchpad_trade(
        {"pool": POOL, "user": USER, "amount0": 21 * 10**18, "amount1": -(100 * 10**6), "sqrt_price_x96": 0},
        201,
        2001,
        "0xsixdec",
        0,
        POOL,
    )

    lp = st.launchpad_tokens.get(TOKEN)
    assert lp is not None
    assert lp.last_price_native == Decimal(10), (
        f"expected the scaled mid of 10 MON per token, got {lp.last_price_native}"
    )


def test_an_eighteen_decimal_quote_is_unchanged_by_the_scaling(monkeypatch):
    token_decimals.prime({WMON: 18, TOKEN: 18})
    st = _state(monkeypatch, WMON)
    nf_events.parse_v2_pair_sync(POOL, [], f"{200 * 10**18:064x}" + f"{1000 * 10**18:064x}")

    st.apply_launchpad_trade(
        {"pool": POOL, "user": USER, "amount0": 21 * 10**18, "amount1": -(100 * 10**18), "sqrt_price_x96": 0},
        201,
        2001,
        "0xeighteen",
        0,
        POOL,
    )

    lp = st.launchpad_tokens.get(TOKEN)
    assert lp is not None
    assert lp.last_price_native == Decimal(200) / Decimal(1000)


def test_a_quote_whose_decimals_are_unknown_skips_the_trade_rather_than_guessing(monkeypatch):
    token_decimals.prime({TOKEN: 18})  # the quote is deliberately absent
    st = _state(monkeypatch, UNKNOWN)
    st.tokenToPrice[UNKNOWN] = Decimal(1)
    lp = st.launchpad_tokens[TOKEN]
    before = lp.last_price_native
    nf_events.parse_v2_pair_sync(POOL, [], f"{200 * 10**6:064x}" + f"{1000 * 10**18:064x}")

    st.apply_launchpad_trade(
        {"pool": POOL, "user": USER, "amount0": 21 * 10**18, "amount1": -(100 * 10**6), "sqrt_price_x96": 0},
        201,
        2001,
        "0xunknownquote",
        0,
        POOL,
    )

    assert lp.last_price_native == before, "an unknown quote must leave the price alone"
    assert lp.tx_count == 0, "and must not book the trade at all"


def test_a_price_in_a_stable_is_converted_into_mon(monkeypatch):
    st = _state(monkeypatch, USDC)
    # 0.2 USDC per token, a dollar per USDC, 0.02 dollars per MON -> 10 MON per token
    assert st._quote_to_native(Decimal("0.2"), USDC) == Decimal(10)
    # a native-equivalent quote is already in MON
    assert st._quote_to_native(Decimal("0.2"), WMON) == Decimal("0.2")
    # and with no rate there is no price, rather than a wrong one
    st.mon_price_usd = Decimal(0)
    assert st._quote_to_native(Decimal("0.2"), USDC) == Decimal(0)
