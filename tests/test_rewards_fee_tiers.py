"""What the terminal charges a wallet, from the status it held when the last week closed.

The fee is applied by the client, so these numbers are the whole enforcement. Anything the backend
cannot answer confidently has to come back as the full fee: unknown must never be cheaper.
"""

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.rewards as rewards  # noqa: E402
import core.storage as storage  # noqa: E402
from tests.test_launchpad_integration import clean, db  # noqa: E402, F401

RAW_URL = os.environ.get("TEST_DATABASE_URL")


def test_the_advertised_tiers_are_the_discounts_off_one_percent():
    assert rewards.FULL_FEE_BPS == 100
    tiers = rewards.status_fee_bps()
    assert tiers == {"bronze": 100, "silver": 95, "gold": 90, "platinum": 80, "diamond": 50}
    # 0%, 5%, 10%, 20%, 50% off
    for status, want in (("bronze", 0), ("silver", 5), ("gold", 10), ("platinum", 20), ("diamond", 50)):
        off = (rewards.FULL_FEE_BPS - tiers[status]) * 100 / rewards.FULL_FEE_BPS
        assert off == want, f"{status} should be {want}% off"


def test_an_unknown_or_missing_status_pays_the_full_fee():
    for status in (None, "", "platinumm", "PLATINUM ", "free", "gold "):
        assert rewards.fee_bps_for_status(status) == rewards.FULL_FEE_BPS, status
    # the ones we do know are matched case-insensitively
    assert rewards.fee_bps_for_status("Diamond") == 50


def test_a_tier_can_only_ever_discount(monkeypatch):
    # a mis-set override must not be able to charge more than the advertised fee, nor go negative
    monkeypatch.setattr(rewards, "_meta_json", lambda key, default: {"diamond": 250, "gold": -10, "silver": 90})
    tiers = rewards.status_fee_bps()
    assert tiers["diamond"] == 50, "over the full fee is refused"
    assert tiers["gold"] == 90, "negative is refused"
    assert tiers["silver"] == 90, "a sane override is taken"


@pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")
def test_the_fee_comes_from_the_last_finalized_week_only(db, clean):  # noqa: F811
    from fastapi.testclient import TestClient

    import api.api
    from api.routes import rewards as rewards_routes

    wallet = "0x" + "7f" * 20
    with storage.db_cursor() as cur:
        storage.ensure_rewards_tables(cur=cur)
        for t in ("crystal_rewards_distributions", "crystal_rewards_weeks", "crystal_rewards_denylist"):
            cur.execute(f"DELETE FROM {t}")

    client = TestClient(api.api.app)
    prefix = rewards_routes.PREFIX

    # nothing has closed yet, so the wallet pays full
    body = client.get(f"{prefix}/fee/{wallet}").json()
    assert body["status"] == "bronze" and body["feeBps"] == 100

    def _week(ws: int, finalized: bool, status: str) -> None:
        with storage.db_cursor() as cur:
            cur.execute(
                "INSERT INTO crystal_rewards_weeks (week_start, week_end, pool, exponent, participants,"
                " total_raw, total_adjusted, finalized, finalized_at) VALUES (%s,%s,0,0,0,0,0,%s,0)"
                " ON CONFLICT (week_start) DO UPDATE SET finalized = EXCLUDED.finalized",
                (ws, ws + 604800, finalized),
            )
            cur.execute(
                "INSERT INTO crystal_rewards_distributions (week_start, wallet, raw_points, adjusted,"
                " share, crystals, rank, participants, status) VALUES (%s,%s,0,0,0,0,1,1,%s)"
                " ON CONFLICT (week_start, wallet) DO UPDATE SET status = EXCLUDED.status",
                (ws, wallet, status),
            )

    _week(1_000_000, True, "gold")
    body = client.get(f"{prefix}/fee/{wallet}").json()
    assert (body["status"], body["feeBps"], body["discountPct"]) == ("gold", 90, 10.0)

    # a newer week that has not finalized must not move the fee yet
    _week(1_600_000, False, "diamond")
    body = client.get(f"{prefix}/fee/{wallet}").json()
    assert body["status"] == "gold", "an open week does not set the fee"

    _week(1_600_000, True, "diamond")
    body = client.get(f"{prefix}/fee/{wallet}").json()
    assert (body["status"], body["feeBps"]) == ("diamond", 50)

    # a denied wallet loses the discount
    with storage.db_cursor() as cur:
        cur.execute("INSERT INTO crystal_rewards_denylist (wallet) VALUES (%s)", (wallet,))
    body = client.get(f"{prefix}/fee/{wallet}").json()
    assert (body["status"], body["feeBps"]) == ("bronze", 100)


@pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")
def test_the_tier_table_is_serialisable_for_the_client(db, clean):  # noqa: F811
    from fastapi.testclient import TestClient

    import api.api
    from api.routes import rewards as rewards_routes

    body = TestClient(api.api.app).get(f"{rewards_routes.PREFIX}/fee-tiers").json()
    assert body["fullFeeBps"] == 100
    assert body["tiers"]["diamond"] == {"feeBps": 50, "discountPct": 50.0}
    assert body["tiers"]["bronze"] == {"feeBps": 100, "discountPct": 0.0}
    assert Decimal(str(body["tiers"]["silver"]["feeBps"])) == 95
