import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h
from core.sequencer import BatchAccumulator, Sequencer

SETTLER = "0xc2d3689cf6ce2859a3ffbc8fe09ab4c8623766b8"
CURVE = "0xa7283d07812a02afb7c09b60f8896bcea3f90ace"
BOT_CONTRACT = "0x415669455d93b755efe7f20ef6f1dbdce7f68f7d"
WALLET = "0x25afd36012fa25336cc56a1b26c56e92dd77f0f3"
TOKEN = "0xbbafe3da53b20fbbdfdcbf8fee74738e07aa7777"
TXH = "0x0b93489149a249897c70bc032e41dc7cea4c7ea09a643e3de742ea8372f4d607"

AMT = 10**21


def _delta(user):
    return dict(
        user_address=user,
        token=TOKEN,
        token_bought_delta=AMT,
        token_sold_delta=0,
        native_spent_delta=AMT,
        native_received_delta=0,
        balance_token_delta=AMT,
        realized_pnl_delta=0,
        trade_count_delta=1,
        buy_count_delta=1,
        sell_count_delta=0,
        last_price_native=1,
    )


def test_the_settler_is_on_the_known_passthrough_list():
    assert SETTLER in h.PASSTHROUGH_ADDRS


def test_a_passthrough_never_accumulates_a_position():
    batch = BatchAccumulator()
    batch.add_position_delta(**_delta(SETTLER))
    assert batch.position_updates == {}


def test_a_real_wallet_still_accumulates_a_position():
    batch = BatchAccumulator()
    batch.add_position_delta(**_delta(WALLET))
    assert (WALLET, TOKEN) in batch.position_updates


def test_resolution_walks_past_a_passthrough_hop():
    # curve -> settler -> wallet, the settler leg must not win
    ordered = [
        {"log_idx": 1, "from": CURVE, "to": SETTLER, "amount": AMT},
        {"log_idx": 2, "from": SETTLER, "to": WALLET, "amount": AMT},
    ]
    maps = {(TXH, TOKEN): {"next": {}, "prev": {}, "ordered": ordered}}
    parsed = {"token": TOKEN, "user": SETTLER, "is_buy": True}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, CURVE, maps)
    assert got == WALLET


def _arb_bot_maps():
    return {
        (TXH, TOKEN): {
            "next": {},
            "prev": {},
            "ordered": [
                {"log_idx": 93, "from": CURVE, "to": SETTLER, "amount": AMT},
                {"log_idx": 97, "from": SETTLER, "to": BOT_CONTRACT, "amount": AMT},
                {"log_idx": 100, "from": BOT_CONTRACT, "to": SETTLER, "amount": AMT},
                {"log_idx": 106, "from": SETTLER, "to": CURVE, "amount": AMT},
            ],
        }
    }


def test_an_arb_bot_lands_on_its_own_contract_not_shared_infrastructure():
    # the wallet that signed this one never touches the token, it trades through a
    # contract. there is no wallet to find, but the credit at least stops being the
    # settler, which every other user routes through too
    parsed = {"token": TOKEN, "user": SETTLER, "is_buy": False}
    got = Sequencer._resolve_trade_user(object.__new__(Sequencer), TXH, parsed, CURVE, _arb_bot_maps())
    assert got == BOT_CONTRACT

    batch = BatchAccumulator()
    batch.add_position_delta(**_delta(got))
    assert (BOT_CONTRACT, TOKEN) in batch.position_updates


def test_adding_an_address_to_the_list_drops_its_positions(monkeypatch):
    monkeypatch.setattr(h, "PASSTHROUGH_ADDRS", [*h.PASSTHROUGH_ADDRS, BOT_CONTRACT])

    batch = BatchAccumulator()
    batch.add_position_delta(**_delta(BOT_CONTRACT))
    assert batch.position_updates == {}
