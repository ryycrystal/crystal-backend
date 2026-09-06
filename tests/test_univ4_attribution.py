import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h
from core.sequencer import Sequencer

POOL_MANAGER = h.UNIV4_POOL_MANAGER_ADDR.lower()
POOL_ID = "0x965e23eaf54b4538492139a6ebe19214ce6ecffc05b90d5303812bc34de822f6"
EXECUTOR = "0xfb78fcae443eb423b59b8c186518c5df94416344"
ROUTER = "0xb92fe925c8b3e4f3f3b0b5d5b2c7e9b1a4d6f8e0"
WALLET = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
TOKEN = "0x405b6330e213ded490240cbcdd64790806827777"
TXH = "0x09131fec043a601c37c359e8d91b8d84954e480c0c22a2138f6fdbc55fbe74c3"

V4_TOKENS = 2917301720048102871806000
V4_NATIVE = 68724686013469231510000
V3_TOKENS = 3939541450744060040000000


def _route_maps():
    ordered = [
        {"log_idx": 120, "from": "0xaa5de197f9c8ee9d2e0b8a7d6c5b4a3f2e1d0c9b", "to": EXECUTOR, "amount": V3_TOKENS},
        {"log_idx": 125, "from": POOL_MANAGER, "to": EXECUTOR, "amount": V4_TOKENS},
        {"log_idx": 126, "from": EXECUTOR, "to": ROUTER, "amount": V4_TOKENS + V3_TOKENS},
        {"log_idx": 128, "from": ROUTER, "to": WALLET, "amount": V4_TOKENS + V3_TOKENS},
    ]
    return {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}


def _sequencer_with_pool(captured):
    seq = object.__new__(Sequencer)
    seq._state = SimpleNamespace(
        v4_pools={POOL_ID: SimpleNamespace(token_addr=TOKEN, token_is_0=False)},
        v3_pools={},
        apply_launchpad_trade=lambda ev, *a, **k: captured.append(ev),
    )
    return seq


def test_a_v4_buy_leg_lands_on_the_wallet_at_the_end_of_the_route():
    captured = []
    parsed = {"pool_id": POOL_ID, "sender": EXECUTOR, "amount0": -V4_NATIVE, "amount1": V4_TOKENS, "sqrt_price_x96": 1}
    _sequencer_with_pool(captured)._apply_univ4_swap(parsed, 101979140, 1_700_000_000, TXH, 124, _route_maps())
    assert len(captured) == 1
    assert captured[0]["user"] == WALLET
    assert captured[0]["pool"] == POOL_ID
    assert captured[0]["amount1"] == -V4_TOKENS


def test_the_bytes32_pool_id_is_not_a_transfer_graph_node():
    # resolving against the pool id cannot see the PoolManager's transfer, so the side is
    # unknown and the executor that called the PoolManager wins by fallback: the bug this
    # file pins
    parsed = {"token": TOKEN, "user": EXECUTOR}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, POOL_ID, _route_maps())
    assert got == EXECUTOR
    parsed = {"token": TOKEN, "user": EXECUTOR, "is_buy": True}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, POOL_MANAGER, _route_maps())
    assert got == WALLET


def test_a_v4_sell_leg_resolves_the_seller():
    captured = []
    ordered = [
        {"log_idx": 10, "from": WALLET, "to": EXECUTOR, "amount": V4_TOKENS},
        {"log_idx": 12, "from": EXECUTOR, "to": POOL_MANAGER, "amount": V4_TOKENS},
    ]
    maps = {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}
    parsed = {"pool_id": POOL_ID, "sender": EXECUTOR, "amount0": V4_NATIVE, "amount1": -V4_TOKENS, "sqrt_price_x96": 1}
    _sequencer_with_pool(captured)._apply_univ4_swap(parsed, 101979140, 1_700_000_000, TXH, 12, maps)
    assert captured[0]["user"] == WALLET
