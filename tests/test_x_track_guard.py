import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.x_track as xt
from core.storage.x_track import handle_from_social_url


class _Req:
    def __init__(self, body=None, admin_token=""):
        self._body = body if body is not None else {}
        self.headers = {"x-admin-token": admin_token} if admin_token else {}

    async def json(self):
        return self._body


def _reset_cache():
    xt._KNOWN_HANDLES = (0.0, frozenset())


def test_reads_never_enroll_handles():
    with patch.object(xt, "_add_tracked") as add:
        with patch.object(xt.storage, "list_x_recent_tweets", return_value=[]):
            asyncio.run(xt.x_tweets_get(usernames="somebody,another"))
            asyncio.run(xt.x_tweets_post(_Req({"usernames": ["somebody"]})))
    add.assert_not_called()


def test_unknown_handle_cannot_be_enrolled_anonymously():
    _reset_cache()
    with patch.object(xt.storage, "list_x_tracked_users", return_value=[]):
        with patch.object(xt.storage, "list_token_social_handles", return_value={"knowncoin"}):
            with patch.object(xt.storage, "add_x_tracked_users") as add:
                with pytest.raises(Exception) as err:
                    asyncio.run(xt.x_track_add(_Req({"usernames": ["randomvictim"]})))
                assert getattr(err.value, "status_code", None) == 403
                add.assert_not_called()


def test_token_linked_handle_is_still_enrolled_anonymously():
    _reset_cache()
    with patch.object(xt.storage, "list_x_tracked_users", return_value=[]):
        with patch.object(xt.storage, "list_token_social_handles", return_value={"knowncoin"}):
            with patch.object(xt.storage, "add_x_tracked_users") as add:
                asyncio.run(xt.x_track_add(_Req({"usernames": ["KnownCoin"]})))
                add.assert_called_once()
                assert [n.lower() for n in add.call_args[0][0]] == ["knowncoin"]


def test_admin_may_enroll_any_handle():
    _reset_cache()
    with patch.object(xt, "ADMIN_TOKEN", "s3cret"):
        with patch.object(xt.storage, "list_x_tracked_users", return_value=[]):
            with patch.object(xt.storage, "list_token_social_handles", return_value=set()):
                with patch.object(xt.storage, "add_x_tracked_users") as add:
                    asyncio.run(xt.x_track_add(_Req({"usernames": ["anyone"]}, admin_token="s3cret")))
                    add.assert_called_once()


def test_already_tracked_handles_stay_allowed():
    _reset_cache()
    with patch.object(xt.storage, "list_x_tracked_users", return_value=["legacyhandle"]):
        with patch.object(xt.storage, "list_token_social_handles", return_value=set()):
            with patch.object(xt.storage, "add_x_tracked_users") as add:
                asyncio.run(xt.x_track_add(_Req({"usernames": ["legacyhandle"]})))
                add.assert_not_called()


def test_social_url_parsing_accepts_profiles_and_rejects_the_rest():
    assert handle_from_social_url("https://x.com/ChrysanMonad") == "chrysanmonad"
    assert handle_from_social_url("https://twitter.com/SomeOne?s=21") == "someone"
    assert handle_from_social_url("https://x.com/i/communities/1745111726582894711") is None
    assert handle_from_social_url("https://x.com/dihveloper/status/1993008477472686111") is None
    assert handle_from_social_url("https://example.com/notx") is None
    assert handle_from_social_url("") is None
    assert handle_from_social_url("https://x.com/waytoolongusernamehere") is None
