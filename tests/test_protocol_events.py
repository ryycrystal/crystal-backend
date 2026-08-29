import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h
from modules import protocol as proto

USER = "b" * 40
TOKEN = "c" * 40


def _topics(topic, *rest):
    return [topic] + ["0x" + "0" * 24 + r for r in rest]


def test_every_contract_event_is_registered_with_a_parser():
    for tag in ("IBD", "IBW", "LPC", "GOV"):
        topics = [t for t, v in h.EVENT_SIGS.items() if v == tag]
        assert len(topics) == 1, f"{tag} should map from exactly one topic"
        assert h.PARSERS.get(tag) is not None, f"{tag} has no parser"
        assert h.accepts_log_for_indexing(tag, h.CONTRACTS["CRYSTAL"].lower()) is True


def test_registered_topics_match_the_contract_signatures():
    assert h.EVENT_SIGS[proto.BALANCE_DEPOSIT_TOPIC] == "IBD"
    assert h.EVENT_SIGS[proto.BALANCE_WITHDRAW_TOPIC] == "IBW"
    assert h.EVENT_SIGS[proto.LAUNCHPAD_PARAMS_TOPIC] == "LPC"
    assert h.EVENT_SIGS[proto.GOV_CHANGED_TOPIC] == "GOV"


def test_deposit_decodes_user_token_and_amount():
    data = f"{7:064x}"
    out = proto.parse_balance_deposit("0xcore", _topics(proto.BALANCE_DEPOSIT_TOPIC, USER, f"{42:040x}", TOKEN), data)
    assert out == {"kind": "deposit", "user": "0x" + USER, "user_id": 42, "token": "0x" + TOKEN, "amount": 7}


def test_withdraw_decodes_with_its_own_kind():
    data = f"{9:064x}"
    out = proto.parse_balance_withdraw("0xcore", _topics(proto.BALANCE_WITHDRAW_TOPIC, USER, f"{1:040x}", TOKEN), data)
    assert out["kind"] == "withdraw"
    assert out["amount"] == 9


def test_balance_events_survive_malformed_logs():
    assert proto.parse_balance_deposit("0xcore", [proto.BALANCE_DEPOSIT_TOPIC], "") is None
    short = _topics(proto.BALANCE_DEPOSIT_TOPIC, USER)
    assert proto.parse_balance_deposit("0xcore", short, "") is None


def test_launchpad_params_decode_in_declaration_order():
    words = [11, 22, 33, 44, 55, 66, 77]
    data = "".join(f"{w:064x}" for w in words)
    out = proto.parse_launchpad_params_changed("0xcore", [proto.LAUNCHPAD_PARAMS_TOPIC], data)
    assert out["kind"] == "launchpad_params_changed"
    assert out["params"]["initial_native_supply"] == 11
    assert out["params"]["graduated_creator_fee_split"] == 77
    assert list(out["params"]) == list(proto.LAUNCHPAD_PARAM_FIELDS)


def test_gov_change_decodes_both_addresses():
    prev, gov = "1" * 40, "2" * 40
    data = "0" * 24 + prev + "0" * 24 + gov
    out = proto.parse_gov_changed("0xcore", [proto.GOV_CHANGED_TOPIC], data)
    assert out == {"kind": "gov_changed", "params": {"previous": "0x" + prev, "gov": "0x" + gov}}
