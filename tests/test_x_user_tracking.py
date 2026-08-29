import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.x_track as xt

KEY = "a" * 64


class _Req:
    def __init__(self, body=None):
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def test_key_must_be_64_hex():
    for bad in ("", "abc", "z" * 64, "a" * 63):
        with pytest.raises(Exception) as err:
            asyncio.run(xt.x_user_track_list(bad))
        assert getattr(err.value, "status_code", None) == 400


def test_user_list_is_scoped_to_the_key():
    with patch.object(xt.storage, "list_user_tracked", return_value=["alice"]) as listed:
        out = asyncio.run(xt.x_user_track_list(KEY))
    assert out == {"tracked": ["alice"]}
    assert listed.call_args[0][0] == KEY


def test_adding_stores_under_the_callers_key_only():
    with patch.object(xt.storage, "list_user_tracked", side_effect=[[], ["newhandle"]]):
        with patch.object(xt.storage, "count_polled_usernames", return_value=10):
            with patch.object(xt.storage, "add_user_tracked") as add:
                out = asyncio.run(xt.x_user_track_add(KEY, _Req({"usernames": ["@NewHandle"]})))
    add.assert_called_once_with(KEY, ["newhandle"])
    assert out == {"tracked": ["newhandle"]}


def test_removing_actually_deletes_rather_than_hiding():
    with patch.object(xt.storage, "list_user_tracked", return_value=[]):
        with patch.object(xt.storage, "remove_user_tracked") as rm:
            asyncio.run(xt.x_user_track_remove(KEY, _Req({"usernames": ["alice"]})))
    rm.assert_called_once()
    assert rm.call_args[0][0] == KEY


def test_per_user_cap_is_enforced():
    existing = [f"user{i}" for i in range(xt.TRACK_MAX_PER_USER)]
    with patch.object(xt.storage, "list_user_tracked", return_value=existing):
        with patch.object(xt.storage, "count_polled_usernames", return_value=0):
            with patch.object(xt.storage, "add_user_tracked") as add:
                with pytest.raises(Exception) as err:
                    asyncio.run(xt.x_user_track_add(KEY, _Req({"usernames": ["onemore"]})))
    assert getattr(err.value, "status_code", None) == 429
    add.assert_not_called()


def test_global_poll_capacity_is_enforced():
    with patch.object(xt.storage, "list_user_tracked", return_value=[]):
        with patch.object(xt.storage, "count_polled_usernames", return_value=xt.TRACK_MAX_USERS):
            with patch.object(xt.storage, "add_user_tracked") as add:
                with pytest.raises(Exception) as err:
                    asyncio.run(xt.x_user_track_add(KEY, _Req({"usernames": ["onemore"]})))
    assert getattr(err.value, "status_code", None) == 429
    add.assert_not_called()


def test_malformed_handles_are_rejected():
    for bad in ("has space", "way_too_long_handle_here", "bad/slash", "semi;colon"):
        with patch.object(xt.storage, "list_user_tracked", return_value=[]):
            with pytest.raises(Exception) as err:
                asyncio.run(xt.x_user_track_add(KEY, _Req({"usernames": [bad]})))
        assert getattr(err.value, "status_code", None) == 400


def test_poller_reads_the_union_of_every_list():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api", "x_track.py")).read()
    assert "storage.list_polled_usernames()" in src
    assert "for username in storage.list_x_tracked_users():" not in src
