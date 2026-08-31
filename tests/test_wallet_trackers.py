import asyncio
import base64
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.routes.trackers as trackers

KEY = "a" * 64
PAYLOAD = {
    "version": 1,
    "iv": base64.b64encode(b"0" * 12).decode(),
    "ciphertext": base64.b64encode(b"1" * 16).decode(),
    "updatedAt": 123456,
}


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def test_tracker_key_must_be_64_hex():
    for bad in ("", "abc", "z" * 64, "a" * 63):
        with pytest.raises(Exception) as err:
            trackers.wallet_tracker_list(bad)
        assert getattr(err.value, "status_code", None) == 400


def test_tracker_payload_is_scoped_to_opaque_key():
    with patch.object(trackers.storage, "get_wallet_tracker_payload", return_value=PAYLOAD) as get:
        out = trackers.wallet_tracker_list(KEY)
    assert out == {"payload": PAYLOAD}
    get.assert_called_once_with(KEY)


def test_encrypted_tracker_payload_is_saved_verbatim():
    with patch.object(trackers.storage, "put_wallet_tracker_payload", return_value=True) as put:
        out = asyncio.run(trackers.wallet_tracker_save(KEY, _Req(PAYLOAD)))
    assert out == {"ok": True, "stored": True}
    put.assert_called_once_with(
        KEY,
        PAYLOAD["version"],
        PAYLOAD["iv"],
        PAYLOAD["ciphertext"],
        PAYLOAD["updatedAt"],
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("iv", base64.b64encode(b"short").decode()),
        ("ciphertext", base64.b64encode(b"short").decode()),
        ("updatedAt", 0),
    ],
)
def test_invalid_tracker_payload_is_rejected(field, value):
    body = {**PAYLOAD, field: value}
    with patch.object(trackers.storage, "put_wallet_tracker_payload") as put:
        with pytest.raises(Exception) as err:
            asyncio.run(trackers.wallet_tracker_save(KEY, _Req(body)))
    assert getattr(err.value, "status_code", None) == 400
    put.assert_not_called()
