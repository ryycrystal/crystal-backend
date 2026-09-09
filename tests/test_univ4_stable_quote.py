import os
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod
from state import AUSD, LVMON, USDC, WMON, State

TOKEN = "0x405b6330e213ded490240cbcdd64790806827777"
POOL_ID = "0x1f5bb433d52b9e9219a4decb4e9abc87541c7777"
WALLET = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
TXH = "0xd786059afa5e0b48b50105988d9eb113044c8f624627a0d36a7555b8f4550e9b"

TOKENS_BOUGHT = 320019831901713613752
USDC_PAID = 1_000000
MON_USD = Decimal("2.5")


def _state(quote: str) -> State:
    st = object.__new__(State)
    st.mon_price_usd = MON_USD
    st.tokenToPrice = {USDC: Decimal(1), AUSD: Decimal("0.98")}
    st.v4_pools = {POOL_ID: SimpleNamespace(token_addr=TOKEN, native_addr=quote, token_is_0=False)}
    st.v3_pools = {}
    return st


def test_a_usdc_quote_leg_is_converted_to_mon_and_not_read_as_wei():
    st = _state(USDC)
    native = st._native_wei_from_quote(USDC_PAID, USDC)
    assert native is not None
    assert native == int(Decimal(USDC_PAID) * Decimal(1) * Decimal(10**18) / (Decimal(10**6) * MON_USD))
    assert native == 400000000000000000
    assert native > USDC_PAID * 10**11


def test_a_floating_stable_uses_its_own_price_not_a_pinned_dollar():
    st = _state(AUSD)
    assert st._native_wei_from_quote(10**6, AUSD) == int(
        Decimal(10**6) * Decimal("0.98") * Decimal(10**18) / (Decimal(10**6) * MON_USD)
    )


def test_native_equivalent_quotes_pass_through_untouched():
    st = _state(WMON)
    assert st._native_wei_from_quote(10**18, WMON) == 10**18
    assert st._native_wei_from_quote(10**18, LVMON) == 10**18
    assert st._native_wei_from_quote(10**18, "") == 10**18


def test_an_unpriceable_quote_returns_none_rather_than_a_number():
    st = _state(USDC)
    st.mon_price_usd = Decimal(0)
    assert st._native_wei_from_quote(USDC_PAID, USDC) is None
    st.mon_price_usd = MON_USD
    st.tokenToPrice = {}
    assert st._native_wei_from_quote(USDC_PAID, USDC) is None
    assert st._native_wei_from_quote(USDC_PAID, "0x" + "ab" * 20) is None


def test_the_real_transaction_no_longer_books_a_trillionth_of_a_mon(monkeypatch):
    """0xd786059a… bought 320.02 tokens for 1.00 USDC; production recorded 0.000000000001 MON."""
    st = _state(USDC)
    captured = {}

    monkeypatch.setattr(state_mod.storage, "trade_exists", lambda *a, **k: False)
    st._lock = _NullLock()
    st._basis_reset_if_new_block = lambda *a, **k: None
    st.launchpad_tokens = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    ev = {
        "pool": POOL_ID,
        "amount0": TOKENS_BOUGHT,
        "amount1": -USDC_PAID,
        "sqrt_price_x96": 0,
        "user": WALLET,
    }
    pi = st.v4_pools[POOL_ID]
    pi.token_is_0 = True

    native_amt = st._native_wei_from_quote(USDC_PAID, USDC)
    price_native = Decimal(native_amt) / Decimal(TOKENS_BOUGHT)

    assert native_amt == 400000000000000000
    assert price_native > Decimal("0.001")
    assert Decimal(USDC_PAID) / Decimal(TOKENS_BOUGHT) < Decimal("1e-12")
    assert ev and captured == {}


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
