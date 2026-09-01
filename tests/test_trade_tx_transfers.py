import os
import sys
import threading
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod
from core import chain as h
from core.sequencer import Sequencer

E = 10**18
TOKEN = "0xtoken"
A = "0xaaaa000000000000000000000000000000000001"
SETTLER = "0xfb2d91483743d7c6097bc58c816bd9026303946a"


class Lp:
    market = ""
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
        self.token_to_v3_pool = {}
        self.v3_pools = {}
        self.launchpad_market_to_token = {}
        self.addressToMarket = {}
        self._basis_overlay = {(a, TOKEN): list(v) for a, v in basis.items()}

    def _basis_for(self, user, token, cur=None):
        return self._basis_overlay.setdefault(((user or "").lower(), token), [0, 0])

    _basis_apply_transfer = state_mod.State._basis_apply_transfer


def run(stub, frm, to, amount, in_trade_tx):
    batch = Batch()
    state_mod.State.apply_token_transfer(
        stub,
        {"token": TOKEN, "from": frm, "to": to, "amount": amount},
        1,
        1,
        "0x",
        cur=None,
        batch=batch,
        in_trade_tx=in_trade_tx,
    )
    return batch


def test_a_trade_tx_transfer_leg_never_drains_basis():
    stub = Stub({A: [100 * E, 100 * E]})
    b = run(stub, A, SETTLER, 100 * E, in_trade_tx=True)
    assert b.calls[A]["token_bought_delta"] == 0
    assert b.calls[A]["cost_basis_delta"] == 0
    assert b.calls[A]["balance_token_delta"] == -100 * E
    assert b.calls[SETTLER]["token_bought_delta"] == 0
    assert stub._basis_overlay[(A, TOKEN)] == [100 * E, 100 * E]


def test_a_plain_transfer_still_moves_basis():
    stub = Stub({A: [100 * E, 100 * E]})
    b = run(stub, A, SETTLER, 60 * E, in_trade_tx=False)
    assert b.calls[A]["token_bought_delta"] == -60 * E
    assert b.calls[A]["cost_basis_delta"] == -60 * E
    assert stub._basis_overlay[(A, TOKEN)] == [40 * E, 40 * E]


def _log(topic, txh):
    return {"topics": [topic], "transactionHash": txh, "address": "0x" + "1" * 40, "data": "0x"}


def test_trade_txs_collect_every_trade_shape():
    by_tag = {v: k for k, v in h.EVENT_SIGS.items()}
    logs = []
    expect = set()
    for i, tag in enumerate(("LT", "TR", "NFB", "NFS", "V2SWAP", "V3SWAP")):
        txh = f"0x{i:064x}"
        logs.append(_log(by_tag[tag], txh))
        expect.add(txh)
    logs.append(_log(by_tag["TF"], "0x" + "e" * 64))
    got = Sequencer._collect_trade_txs(object.__new__(Sequencer), logs)
    assert got == expect
