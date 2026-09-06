import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.sequencer import Sequencer

CORE = "0x1ab7ea1880a3f9d1a3e2c4b5d6e7f8091a2b3c4d"
SETTLER = "0xc2d3689cf6ce2859a3ffbc8fe09ab4c8623766b8"
WALLET = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
TOKEN = "0x8e74f6e943a7a28605ddd59945bec63a8919f5e2"
MARKET = "0x9f1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c"
TXH = "0x30ddefc3b4a5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f"
AMT = 933_619_875 * 10**18


def _seq():
    seq = object.__new__(Sequencer)
    seq._state = SimpleNamespace(addressToMarket={MARKET: SimpleNamespace(baseAddress=TOKEN)}, v3_pools={})
    return seq


def test_a_routed_order_book_fill_is_credited_to_the_wallet_not_the_settler():
    ordered = [
        {"log_idx": 50, "from": CORE, "to": SETTLER, "amount": AMT},
        {"log_idx": 52, "from": SETTLER, "to": WALLET, "amount": AMT},
    ]
    maps = {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}
    parsed = {"market": MARKET, "user": SETTLER, "is_buy": True, "amount_in": 1, "amount_out": AMT}
    seq = _seq()
    got = seq._resolve_trade_user(TXH, {**parsed, "token": seq._market_base_token(parsed)}, CORE, maps)
    assert got == WALLET


def test_without_the_market_token_the_resolver_can_only_fall_back():
    ordered = [
        {"log_idx": 50, "from": CORE, "to": SETTLER, "amount": AMT},
        {"log_idx": 52, "from": SETTLER, "to": WALLET, "amount": AMT},
    ]
    maps = {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}
    parsed = {"market": MARKET, "user": SETTLER, "is_buy": True}
    assert _seq()._resolve_trade_user(TXH, parsed, CORE, maps) == SETTLER


def test_an_unknown_market_yields_no_token_and_keeps_the_fallback():
    seq = _seq()
    assert seq._market_base_token({"market": "0x" + "ab" * 20}) == ""
