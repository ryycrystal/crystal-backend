import os
import sys
import threading
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod  # noqa: E402

E = 10**18
TOKEN = "0xtoken"
A = "0xaaaa000000000000000000000000000000000001"
B = "0xbbbb000000000000000000000000000000000002"
POOL = "0xpool00000000000000000000000000000000003"
MARKET = "0xmarket000000000000000000000000000000004"


class Lp:
    market = MARKET
    last_price_native = Decimal("0.000001")


class Batch:
    def __init__(self):
        self.calls = {}

    def add_position_delta(self, **kw):
        self.calls[kw["user_address"]] = kw


class Stub:
    def __init__(self, basis):
        self._lock = threading.Lock()
        self.launchpad_tokens = {TOKEN: Lp()}
        self.token_to_v3_pool = {TOKEN: POOL}
        self.v3_pools = {POOL: object()}
        self.launchpad_market_to_token = {MARKET: TOKEN}
        self.addressToMarket = {}
        self._basis_overlay = {(a, TOKEN): list(v) for a, v in basis.items()}

    def _basis_for(self, user, token, cur=None):
        return self._basis_overlay.setdefault(((user or "").lower(), token), [0, 0])

    _basis_apply_transfer = state_mod.State._basis_apply_transfer


def run(stub, frm, to, amount):
    batch = Batch()
    state_mod.State.apply_token_transfer(
        stub, {"token": TOKEN, "from": frm, "to": to, "amount": amount}, 1, 1, "0x", cur=None, batch=batch
    )
    return batch


def test_basis_moves_proportionally_with_the_tokens():
    stub = Stub({A: [100 * E, 100 * E]})
    b = run(stub, A, B, 60 * E)
    assert b.calls[A]["token_bought_delta"] == -60 * E
    assert b.calls[A]["cost_basis_delta"] == -60 * E
    assert b.calls[B]["token_bought_delta"] == 60 * E
    assert b.calls[B]["cost_basis_delta"] == 60 * E


def test_total_basis_is_conserved():
    stub = Stub({A: [100 * E, 250 * E]})
    b = run(stub, A, B, 40 * E)
    assert b.calls[A]["cost_basis_delta"] + b.calls[B]["cost_basis_delta"] == 0
    assert b.calls[B]["cost_basis_delta"] == 100 * E


def test_overlay_is_updated_for_both_sides():
    stub = Stub({A: [100 * E, 100 * E]})
    run(stub, A, B, 60 * E)
    assert stub._basis_overlay[(A, TOKEN)] == [40 * E, 40 * E]
    assert stub._basis_overlay[(B, TOKEN)] == [60 * E, 60 * E]


def test_balance_still_moves_the_full_amount():
    stub = Stub({A: [100 * E, 100 * E]})
    b = run(stub, A, B, 60 * E)
    assert b.calls[A]["balance_token_delta"] == -60 * E
    assert b.calls[B]["balance_token_delta"] == 60 * E


def test_swap_legs_move_no_basis():
    for venue in (POOL, MARKET):
        stub = Stub({A: [100 * E, 100 * E]})
        b = run(stub, A, venue, 60 * E)
        assert b.calls[A]["cost_basis_delta"] == 0
        assert b.calls[A]["token_bought_delta"] == 0
        assert b.calls[A]["balance_token_delta"] == -60 * E


def test_transfer_of_untracked_tokens_moves_no_basis():
    stub = Stub({A: [0, 0]})
    b = run(stub, A, B, 60 * E)
    assert b.calls[A]["cost_basis_delta"] == 0
    assert b.calls[B]["token_bought_delta"] == 0


def test_move_never_exceeds_what_the_sender_acquired():
    stub = Stub({A: [10 * E, 10 * E]})
    b = run(stub, A, B, 60 * E)
    assert b.calls[A]["token_bought_delta"] == -10 * E
    assert b.calls[A]["cost_basis_delta"] == -10 * E
    assert b.calls[A]["balance_token_delta"] == -60 * E


def test_realized_pnl_is_untouched_by_a_transfer():
    stub = Stub({A: [100 * E, 100 * E]})
    b = run(stub, A, B, 60 * E)
    assert b.calls[A]["realized_pnl_delta"] == Decimal(0)
    assert b.calls[B]["realized_pnl_delta"] == Decimal(0)
