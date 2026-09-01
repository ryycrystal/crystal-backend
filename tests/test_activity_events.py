import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h


def test_claim_events_are_accepted_from_the_crystal_contract():
    assert h.accepts_log_for_indexing("REFCLAIM", h.CONTRACTS["CRYSTAL"].lower()) is True


def test_claim_events_are_not_accepted_from_the_referral_manager():
    if h.CONTRACTS["REFERRALS"].lower() == h.CONTRACTS["CRYSTAL"].lower():
        return
    assert h.accepts_log_for_indexing("REFCLAIM", h.CONTRACTS["REFERRALS"].lower()) is False


def test_referral_manager_matches_the_contract_the_app_writes_to():
    # the app binds referrals through this manager; indexing any other address
    # silently produces an empty referral_bindings table and no referral activity
    assert h.CONTRACTS["REFERRALS"].lower() == "0x1ab7ea187cee63cf01bbd8fa8837c748a769f8df"


def test_referral_bindings_still_come_only_from_the_referral_manager():
    assert h.accepts_log_for_indexing("REF", h.CONTRACTS["REFERRALS"].lower()) is True
    if h.CONTRACTS["REFERRALS"].lower() != h.CONTRACTS["CRYSTAL"].lower():
        assert h.accepts_log_for_indexing("REF", h.CONTRACTS["CRYSTAL"].lower()) is False


def _state_with_market(market: str):
    from state import State

    st = State.__new__(State)
    st._lock = threading.RLock()
    st.addressToMarket = {market: type("M", (), {"marketType": 2})()}
    return st


def test_lp_mint_records_the_depositor():
    market = "0x" + "1" * 40
    lp = "0x" + "2" * 40
    st = _state_with_market(market)
    with patch.object(sys.modules["state"], "storage") as mock_storage:
        st.apply_pool_liquidity(
            "mint", 100, 1000, {"market": market, "sender": lp, "amountQuote": 5, "amountBase": 7}, market, "0xtx", 3
        )
        kwargs = mock_storage.insert_pool_liquidity_event.call_args.kwargs
    assert kwargs["user_address"] == lp
    assert kwargs["kind"] == "mint"


def test_lp_burn_records_the_recipient_and_falls_back_to_the_sender():
    market = "0x" + "1" * 40
    sender = "0x" + "3" * 40
    to = "0x" + "4" * 40
    st = _state_with_market(market)
    with patch.object(sys.modules["state"], "storage") as mock_storage:
        st.apply_pool_liquidity(
            "burn",
            100,
            1000,
            {"market": market, "sender": sender, "to": to, "amountQuote": 5, "amountBase": 7},
            market,
            "0xtx",
            3,
        )
        assert mock_storage.insert_pool_liquidity_event.call_args.kwargs["user_address"] == to

        st.apply_pool_liquidity(
            "burn",
            100,
            1000,
            {"market": market, "sender": sender, "amountQuote": 5, "amountBase": 7},
            market,
            "0xtx",
            4,
        )
        assert mock_storage.insert_pool_liquidity_event.call_args.kwargs["user_address"] == sender
