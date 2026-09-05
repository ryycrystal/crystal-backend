import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import api.api  # noqa: E402, F401
from api.routes.dexscreener import _checksum, dex_pair  # noqa: E402
from tests.test_launchpad_integration import (  # noqa: E402
    TOKEN,
    WMON,
    _create,
    _new_state,
    clean,  # noqa: F401  autouse truncation between tests
    db,  # noqa: F401
)


def test_pair_created_fields_come_from_token_creation_row(db):
    _create(_new_state(), blk=123, ts=1756123456)
    pair = dex_pair(id=TOKEN)["pair"]
    assert pair["id"] == _checksum(TOKEN)
    assert pair["asset0Id"] == _checksum(TOKEN)
    assert pair["asset1Id"] == _checksum(WMON)
    assert pair["createdAtBlockNumber"] == 123
    assert pair["createdAtBlockTimestamp"] == 1756123456
