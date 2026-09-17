import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from eth_utils import keccak  # noqa: E402

from modules import protocol as proto  # noqa: E402

SIGNATURE = "LaunchpadParamsChanged(bool,uint112,uint256,uint256,uint256,uint256,uint256,uint256)"
WORDS = [0, 200_000 * 10**18, 99_000, 10, 100 * 10**18, 99_970, 99_995, 50]


def _hex(words):
    return "".join(f"{w:064x}" for w in words)


def test_topic_is_the_contract_event_signature():
    assert proto.LAUNCHPAD_PARAMS_TOPIC == "0x" + keccak(text=SIGNATURE).hex()


def test_layout_matches_the_solidity_struct_order():
    assert proto.LAUNCHPAD_PARAM_FIELDS == (
        "is_token_creation_paused",
        "initial_native_supply",
        "launchpad_fee",
        "creator_fee_split",
        "graduated_min_size",
        "graduated_taker_fee",
        "graduated_maker_rebate",
        "graduated_creator_fee_split",
    )


def test_an_event_decodes_every_field_in_place():
    params = proto.parse_launchpad_params_changed("0x0", [proto.LAUNCHPAD_PARAMS_TOPIC], _hex(WORDS))["params"]
    assert params["is_token_creation_paused"] == 0
    assert params["initial_native_supply"] == 200_000 * 10**18
    assert params["launchpad_fee"] == 99_000
    assert params["creator_fee_split"] == 10
    assert params["graduated_min_size"] == 100 * 10**18
    assert params["graduated_creator_fee_split"] == 50


def test_the_params_event_is_indexed_rather_than_dropped():
    from core import chain as h

    assert h.EVENT_SIGS[proto.LAUNCHPAD_PARAMS_TOPIC] == "LPC"
    assert proto.LAUNCHPAD_PARAMS_TOPIC in h.TOPICS


@pytest.fixture
def fetch(monkeypatch):
    import state

    def run(words):
        state._LAUNCHPAD_PARAMS_CACHE.clear()
        monkeypatch.setattr(state, "_eth_call", lambda to, data: "0x" + _hex(words))
        try:
            return state._fetch_launchpad_initial_native_supply()
        finally:
            state._LAUNCHPAD_PARAMS_CACHE.clear()

    return run


def test_fetch_reads_the_supply_from_the_second_word(fetch):
    assert fetch(WORDS) == 200_000 * 10**18


def test_fetch_never_returns_the_paused_flag_as_a_supply(fetch):
    assert fetch([1, *WORDS[1:]]) == 200_000 * 10**18
