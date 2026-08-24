import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.routes.referrals as ref_api
import core.storage.referrals as ref_storage
from core import chain as h
from modules.referrals import REFERRAL_TOPIC, parse_referral


# the binding event decodes indexed referrer and unindexed referee
def test_parse_referral_decodes_both_addresses():
    referrer = "a" * 40
    referee = "b" * 40
    tops = [REFERRAL_TOPIC, "0x" + "0" * 24 + referrer]
    data = "0" * 24 + referee
    out = parse_referral("0xmanager", tops, data)
    assert out == {"referrer": "0x" + referrer, "referee": "0x" + referee}


# a cleared binding decodes the zero referrer
def test_parse_referral_zero_referrer():
    tops = [REFERRAL_TOPIC, "0x" + "0" * 64]
    data = "0" * 24 + "c" * 40
    out = parse_referral("0xmanager", tops, data)
    assert out["referrer"] == "0x" + "0" * 40


# referral logs are only accepted from the manager address
def test_accepts_referral_log_only_from_manager():
    assert h.accepts_log_for_indexing("REF", h.CONTRACTS["REFERRALS"]) is True
    assert h.accepts_log_for_indexing("REF", "0x" + "1" * 40) is False


# the full wiring: topic subscribed, tagged, and parser registered
def test_referral_wiring_is_complete():
    assert REFERRAL_TOPIC in h.TOPICS
    assert h.EVENT_SIGS[REFERRAL_TOPIC] == "REF"
    assert h.PARSERS["REF"] is parse_referral


# malformed logs decode to zero addresses instead of raising
def test_parse_referral_malformed_inputs():
    out = parse_referral("0xmanager", [REFERRAL_TOPIC], "")
    assert out == {"referrer": "0x" + "0" * 40, "referee": "0x" + "0" * 40}
    out = parse_referral("0xmanager", [REFERRAL_TOPIC, "0x" + "0" * 24 + "a" * 40], "deadbeef")
    assert out["referee"] == "0x" + "0" * 40


# a zero referee from a malformed log must not create a binding row
def test_apply_referral_skips_zero_referee():
    from state import State

    st = State.__new__(State)
    with patch.object(sys.modules["state"], "storage") as mock_storage:
        st.apply_referral(1, 2, {"referee": "0x" + "0" * 40, "referrer": "0x" + "a" * 40})
        st.apply_referral(1, 2, {})
        mock_storage.upsert_referral_binding.assert_not_called()
        st.apply_referral(5, 6, {"referee": "0x" + "b" * 40, "referrer": "0x" + "a" * 40}, log_idx=3)
        mock_storage.upsert_referral_binding.assert_called_once_with(
            "0x" + "b" * 40, "0x" + "a" * 40, 5, 3, 6, cur=None
        )


# the binding upsert refuses to overwrite a newer event with an older one
def test_upsert_binding_sql_guards_ordering():
    cur = MagicMock()
    ref_storage.upsert_referral_binding("0xB", "0xA", 100, 2, 123, cur=cur)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "ON CONFLICT (referee) DO UPDATE" in sql
    assert ">= (referral_bindings.block_number, referral_bindings.log_index)" in sql
    assert params == ("0xb", "0xa", 100, 2, 123)


# the reward journal accrues increases into earned and rejects stale observations
def test_record_reward_sql_journals_deltas():
    cur = MagicMock()
    with patch.object(ref_storage, "db_cursor") as dbc:
        dbc.return_value.__enter__.return_value = cur
        ref_storage.record_referral_reward("0xA", "0xT", 500, 999)
    sql = cur.execute.call_args[0][0]
    assert "GREATEST(EXCLUDED.claimable - referral_rewards.claimable, 0)" in sql
    assert "WHERE EXCLUDED.updated_at >= referral_rewards.updated_at" in sql
    assert cur.execute.call_args[0][1] == ("0xa", "0xt", 500, 500, 999)


# the summary endpoint shapes bindings and rewards and rejects bad addresses
def test_referral_summary_shape():
    class _FakeState:
        tokenToPrice = {"0xt": 2.0}

    with patch.object(
        ref_api.storage, "get_referral_binding", return_value=("0xAAA0000000000000000000000000000000000abc", 5, 111)
    ):
        with patch.object(ref_api.storage, "list_referees", return_value=[("0xDEF", 222)]):
            with patch.object(
                ref_api.storage, "get_referral_rewards", return_value=[("0xT", 1_000_000, 2_500_000, 333)]
            ):
                with patch.object(ref_api, "_quote_decimals", return_value={"0xt": 6}):
                    with patch.object(ref_api, "_price_state", return_value=_FakeState()):
                        out = ref_api.referral_summary("0x" + "9" * 40)
    assert out["ok"] is True
    assert out["referrer"] == "0xaaa0000000000000000000000000000000000abc"
    assert out["referredCount"] == 1
    assert out["referees"] == [{"address": "0xdef", "since": 222}]
    assert out["rewards"] == [
        {
            "token": "0xt",
            "claimable": "1000000",
            "earned": "2500000",
            "claimableUsd": 2.0,
            "earnedUsd": 5.0,
            "updatedAt": 333,
        }
    ]
    assert out["totalClaimableUsd"] == 2.0
    assert out["totalEarnedUsd"] == 5.0

    for bad in ("nothex", "0x" + "z" * 40, "0x" + "0" * 40):
        try:
            ref_api.referral_summary(bad)
            raise AssertionError("expected 400")
        except Exception as e:
            assert getattr(e, "status_code", None) == 400


# a user with no binding and no rewards gets nulls and empty lists
def test_referral_summary_empty_user():
    with patch.object(ref_api.storage, "get_referral_binding", return_value=None):
        with patch.object(ref_api.storage, "list_referees", return_value=[]):
            with patch.object(ref_api.storage, "get_referral_rewards", return_value=[]):
                out = ref_api.referral_summary("0x" + "8" * 40)
    assert out["referrer"] is None
    assert out["referredCount"] == 0
    assert out["rewards"] == []
    assert out["totalEarnedUsd"] == 0.0


LADDER = [
    (0, "Basic", 0, 1, 1000),
    (1, "Bronze", 10000, 1, 1000),
    (2, "Silver", 100000, 2, 2000),
    (3, "Gold", 1000000, 5, 3000),
    (4, "Diamond", 10000000, 10, 4000),
]


# the ladder is served as configured, thresholds and benefits included
def test_tier_ladder_shape():
    with patch.object(ref_api.storage, "list_volume_tiers", return_value=LADDER):
        with patch.object(ref_api.storage, "tier_window_days", return_value=30):
            out = ref_api.volume_tier_ladder.__wrapped__()
    assert out["ok"] is True
    assert out["windowDays"] == 30
    assert [t["name"] for t in out["tiers"]] == ["Basic", "Bronze", "Silver", "Gold", "Diamond"]
    assert [t["minVolumeUsd"] for t in out["tiers"]] == [0, 10000, 100000, 1000000, 10000000]
    assert out["tiers"][2]["referralCommissionPercent"] == 20.0


# volume below the first paid threshold stays on the basic tier
def test_wallet_below_first_threshold_is_basic():
    with patch.object(ref_api.storage, "list_volume_tiers", return_value=LADDER):
        with patch.object(ref_api.storage, "tier_window_days", return_value=30):
            with patch.object(ref_api.storage, "wallet_launchpad_volume_usd", return_value=(2500, 7)):
                out = ref_api.volume_tier_for_wallet.__wrapped__("0x" + "1" * 40)
    assert out["tier"]["name"] == "Basic"
    assert out["nextTier"]["name"] == "Bronze"
    assert out["volumeUsd"] == 2500
    assert out["tradeCount"] == 7
    assert out["remainingUsd"] == 7500
    assert out["progressBps"] == 2500


# a wallet between two thresholds earns the lower one and progresses toward the next
def test_wallet_between_thresholds():
    with patch.object(ref_api.storage, "list_volume_tiers", return_value=LADDER):
        with patch.object(ref_api.storage, "tier_window_days", return_value=30):
            with patch.object(ref_api.storage, "wallet_launchpad_volume_usd", return_value=(550000, 900)):
                out = ref_api.volume_tier_for_wallet.__wrapped__("0x" + "2" * 40)
    assert out["tier"]["name"] == "Silver"
    assert out["nextTier"]["name"] == "Gold"
    assert out["remainingUsd"] == 450000
    assert out["progressBps"] == 5000


# the top tier has nothing above it and reads as complete
def test_wallet_at_top_tier_has_no_next():
    with patch.object(ref_api.storage, "list_volume_tiers", return_value=LADDER):
        with patch.object(ref_api.storage, "tier_window_days", return_value=30):
            with patch.object(ref_api.storage, "wallet_launchpad_volume_usd", return_value=(12_000_000, 5000)):
                out = ref_api.volume_tier_for_wallet.__wrapped__("0x" + "3" * 40)
    assert out["tier"]["name"] == "Diamond"
    assert out["nextTier"] is None
    assert out["remainingUsd"] == 0
    assert out["progressBps"] == 10000


# thresholds are data, so an edited ladder moves wallets without a code change
def test_edited_thresholds_change_the_answer():
    edited = [
        (0, "Basic", 0, 1, 1000),
        (1, "Bronze", 500, 1, 1000),
        (2, "Silver", 1000, 2, 2000),
    ]
    with patch.object(ref_api.storage, "list_volume_tiers", return_value=edited):
        with patch.object(ref_api.storage, "tier_window_days", return_value=30):
            with patch.object(ref_api.storage, "wallet_launchpad_volume_usd", return_value=(750, 3)):
                out = ref_api.volume_tier_for_wallet.__wrapped__("0x" + "4" * 40)
    assert out["tier"]["name"] == "Bronze"
    assert out["nextTier"]["name"] == "Silver"


# a zero volume wallet still resolves rather than returning a null tier
def test_zero_volume_wallet_resolves():
    with patch.object(ref_api.storage, "list_volume_tiers", return_value=LADDER):
        with patch.object(ref_api.storage, "tier_window_days", return_value=30):
            with patch.object(ref_api.storage, "wallet_launchpad_volume_usd", return_value=(0, 0)):
                out = ref_api.volume_tier_for_wallet.__wrapped__("0x" + "5" * 40)
    assert out["tier"]["name"] == "Basic"
    assert out["volumeUsd"] == 0
    assert out["progressBps"] == 0


# a bad address is rejected before any query runs
def test_tier_endpoint_rejects_bad_address():
    for bad in ("nothex", "0x" + "z" * 40, "0x" + "0" * 40):
        try:
            ref_api.volume_tier_for_wallet.__wrapped__(bad)
            raise AssertionError("expected 400")
        except Exception as e:
            assert getattr(e, "status_code", None) == 400
