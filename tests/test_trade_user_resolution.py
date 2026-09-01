import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.sequencer import Sequencer

# the chipotle sell that showed the router as the trader on crystal.fun
TRADER = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
ROUTER = "0x1ab7ea187cee63cf01bbd8fa8837c748a769f8df"
CORE = "0x6eb2af5fc575689053ac9b413220cabfd01a2f9a"
TOKEN = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
TXH = "0x215cf615bb1f24c1265be1702bc3d56c4ea21f86629e18087d3bf469ce96649f"

AMT = 933619875241474959734221736


def _maps():
    ordered = [
        {"log_idx": 2, "from": TRADER, "to": ROUTER, "amount": AMT},
        {"log_idx": 6, "from": ROUTER, "to": CORE, "amount": AMT},
    ]
    return {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}


def _resolve(parsed):
    return Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, CORE, _maps())


def test_a_routed_sell_credits_the_wallet_not_the_router():
    parsed = {"token": TOKEN, "user": ROUTER, "is_buy": False}
    assert _resolve(parsed) == TRADER


def test_a_routed_buy_credits_the_wallet_not_the_router():
    ordered = [
        {"log_idx": 2, "from": CORE, "to": ROUTER, "amount": AMT},
        {"log_idx": 6, "from": ROUTER, "to": TRADER, "amount": AMT},
    ]
    maps = {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}
    parsed = {"token": TOKEN, "user": ROUTER, "is_buy": True}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, CORE, maps)
    assert got == TRADER


def test_a_direct_trade_is_left_alone():
    ordered = [{"log_idx": 2, "from": TRADER, "to": CORE, "amount": AMT}]
    maps = {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}
    parsed = {"token": TOKEN, "user": TRADER, "is_buy": False}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, CORE, maps)
    assert got == TRADER


def test_without_transfers_the_event_caller_is_kept():
    parsed = {"token": TOKEN, "user": ROUTER, "is_buy": False}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, CORE, {})
    assert got == ROUTER


def test_every_trade_tag_resolves_the_real_wallet():
    src = inspect.getsource(Sequencer._process_block_inner)
    # TR was the one trade tag that skipped resolution, which is why spot trades
    # on crystal.fun showed the settler in the trader column
    tr_branch = src.split('elif tag == "TR":')[1].split("elif tag ==")[0]
    assert "_resolve_trade_user" in tr_branch
