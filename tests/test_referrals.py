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


# the binding upsert refuses to overwrite a newer event with an older one
def test_upsert_binding_sql_guards_ordering():
    cur = MagicMock()
    ref_storage.upsert_referral_binding("0xB", "0xA", 100, 2, 123, cur=cur)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "ON CONFLICT (referee) DO UPDATE" in sql
    assert ">= (referral_bindings.block_number, referral_bindings.log_index)" in sql
    assert params == ("0xb", "0xa", 100, 2, 123)


# the reward journal accrues increases into earned and never subtracts on claims
def test_record_reward_sql_journals_deltas():
    cur = MagicMock()
    with patch.object(ref_storage, "db_cursor") as dbc:
        dbc.return_value.__enter__.return_value = cur
        ref_storage.record_referral_reward("0xA", "0xT", 500, 999)
    sql = cur.execute.call_args[0][0]
    assert "GREATEST(EXCLUDED.claimable - referral_rewards.claimable, 0)" in sql
    assert cur.execute.call_args[0][1] == ("0xa", "0xt", 500, 500, 999)


# the summary endpoint shapes bindings and rewards and rejects bad addresses
def test_referral_summary_shape():
    with patch.object(
        ref_api.storage, "get_referral_binding", return_value=("0xAAA0000000000000000000000000000000000abc", 5, 111)
    ):
        with patch.object(ref_api.storage, "list_referees", return_value=[("0xDEF", 222)]):
            with patch.object(ref_api.storage, "get_referral_rewards", return_value=[("0xT", 10, 25, 333)]):
                with patch.object(ref_api, "_quote_decimals", return_value={"0xt": 6}):
                    with patch.object(ref_api, "_cached_state", side_effect=RuntimeError("no db")):
                        out = ref_api.referral_summary("0x" + "9" * 40)
    assert out["ok"] is True
    assert out["referrer"] == "0xaaa0000000000000000000000000000000000abc"
    assert out["referredCount"] == 1
    assert out["referees"] == [{"address": "0xdef", "since": 222}]
    assert out["rewards"] == [
        {"token": "0xt", "claimable": "10", "earned": "25", "claimableUsd": 0.0, "earnedUsd": 0.0, "updatedAt": 333}
    ]
    assert out["totalClaimableUsd"] == 0.0
    assert out["totalEarnedUsd"] == 0.0

    try:
        ref_api.referral_summary("nothex")
        raise AssertionError("expected 400")
    except Exception as e:
        assert getattr(e, "status_code", None) == 400
