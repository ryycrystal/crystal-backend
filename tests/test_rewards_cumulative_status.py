"""Status ranks on every point a wallet has ever earned; crystals still pay on the week's own.

Also covers the second, independent way to hold a status: a granted floor that qualified referrals
raise, which never lowers what the percentile earned and is never lost.
"""

import os
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="set TEST_DATABASE_URL")

import core.storage as storage  # noqa: E402
from tests.test_launchpad_integration import (  # noqa: E402
    clean,  # noqa: F401
    db,  # noqa: F401
)

LA = ZoneInfo("America/Los_Angeles")
WEEK1 = int(datetime(2026, 9, 16, 0, 0, tzinfo=LA).timestamp())

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
C = "0x" + "c3" * 20
KOL = "0x" + "d4" * 20
TOK = "0x" + "ee" * 20

TABLES = (
    "crystal_rewards_contrib",
    "crystal_rewards_weeks",
    "crystal_rewards_distributions",
    "crystal_rewards_balances",
    "crystal_rewards_milestones",
    "crystal_rewards_grants",
    "crystal_rewards_status_overrides",
    "crystal_rewards_referral_progress",
    "crystal_rewards_referral_quals",
    "referral_bindings",
    "launchpad_trades",
)


@pytest.fixture(autouse=True)
def _rw(db, clean):
    import core.rewards as rewards

    with storage.db_cursor() as cur:
        storage.ensure_rewards_tables(cur=cur)
        for t in TABLES:
            cur.execute(f"DELETE FROM {t}")
        cur.execute("DELETE FROM launchpad_kv WHERE key LIKE 'rewards_%'")
    storage.set_meta("rewards_program_start", str(WEEK1))
    storage.set_meta("rewards_vault_start", str(WEEK1))
    storage.set_meta("rewards_pool", "10000")
    storage.set_meta("rewards_referral_qualify_points", "10000")
    yield rewards


def _week(n: int) -> int:
    import core.rewards as rewards

    ws = WEEK1
    for _ in range(n - 1):
        ws = rewards.bucket_end(ws)
    return ws


def _points(week: int, wallet: str, points: float) -> None:
    with storage.db_cursor() as cur:
        storage.add_rewards_contrib(cur, week, wallet, week + 1, points=Decimal(str(points)))


def _unblock(rewards, week_end: int) -> None:
    """The close refuses until every source has passed the week's end."""
    storage.set_meta("rewards_wm_vault_hour", str(week_end))
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO launchpad_tokens (token, creator, name, symbol, source, created_block, created_at) "
            "VALUES (%s, %s, 'T', 'T', 0, 1, %s) ON CONFLICT (token) DO NOTHING",
            (TOK, A, WEEK1 - 100),
        )
    storage.insert_trade(
        block_number=900_000 + week_end % 1000,
        log_index=0,
        timestamp=week_end + 5,
        token=TOK,
        user_address="0x" + "99" * 20,
        is_buy=True,
        native_amount=0,
        token_amount=0,
        usd_amount=0,
        price_native=0,
        txhash="0x" + f"{week_end:064x}"[:64],
    )
    storage.set_meta("rewards_wm_launchpad", "99999999")


def _dist(week: int) -> dict:
    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT wallet, crystals, rank, status, cum_points, cum_rank, earned_status "
            "FROM crystal_rewards_distributions WHERE week_start = %s",
            (week,),
        )
        return {
            w: {
                "crystals": float(c),
                "rank": int(r),
                "status": s,
                "cum_points": float(cp),
                "cum_rank": int(cr),
                "earned": es,
            }
            for w, c, r, s, cp, cr, es in cur.fetchall()
        }


def test_status_ranks_on_cumulative_while_crystals_pay_on_the_week(_rw):
    rewards = _rw
    w1, w2 = _week(1), _week(2)

    # week one: A dominates
    _points(w1, A, 10_000)
    _points(w1, B, 100)
    _unblock(rewards, rewards.bucket_end(w1))
    assert rewards.close_due_weeks(now_ts=rewards.bucket_end(w1) + 60) == [w1]

    # week two: B dominates the week, but A is still far ahead overall
    _points(w2, A, 100)
    _points(w2, B, 900)
    _unblock(rewards, rewards.bucket_end(w2))
    assert rewards.close_due_weeks(now_ts=rewards.bucket_end(w2) + 60) == [w2]

    d = _dist(w2)
    assert d[B]["crystals"] > d[A]["crystals"], "the week's crystals follow the week's points"
    assert d[B]["rank"] == 1, "B won week two"
    assert d[A]["cum_points"] == pytest.approx(10_100)
    assert d[B]["cum_points"] == pytest.approx(1_000)
    assert d[A]["cum_rank"] == 1, "A leads on everything earned so far"
    assert d[B]["cum_rank"] == 2
    assert d[A]["status"] == "diamond", "status follows the cumulative rank, not the week's"


def test_a_granted_floor_lifts_a_wallet_the_percentile_left_at_the_bottom(_rw):
    rewards = _rw
    w1 = _week(1)
    for i in range(30):
        _points(w1, "0x" + f"{i:02x}" * 20, 1000 - i)
    _points(w1, KOL, 1)
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_rewards_status_overrides (wallet, floor_status, is_kol, granted_at) "
            "VALUES (%s, 'gold', TRUE, %s)",
            (KOL, w1),
        )
    _unblock(rewards, rewards.bucket_end(w1))
    assert rewards.close_due_weeks(now_ts=rewards.bucket_end(w1) + 60) == [w1]

    d = _dist(w1)
    assert d[KOL]["earned"] == "bronze", "last place on merit"
    assert d[KOL]["status"] == "gold", "and gold by grant"


def test_the_kol_ladder_pays_platinum_at_ten_referrals_and_diamond_at_twenty_five(_rw):
    rewards = _rw
    w1 = _week(1)
    _points(w1, KOL, 1)
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_rewards_status_overrides (wallet, floor_status, is_kol, granted_at) "
            "VALUES (%s, 'gold', TRUE, %s)",
            (KOL, w1),
        )
        for i in range(10):
            cur.execute(
                "INSERT INTO crystal_rewards_referral_quals (referee, referrer, points, qualified_at) "
                "VALUES (%s, %s, 10000, %s)",
                ("0x" + f"{i:02x}" * 20, KOL, w1),
            )
        floors = rewards._floor_status_for(cur, [KOL], set())
        assert floors[KOL] == "platinum", "ten qualified referrals"

        for i in range(10, 25):
            cur.execute(
                "INSERT INTO crystal_rewards_referral_quals (referee, referrer, points, qualified_at) "
                "VALUES (%s, %s, 10000, %s)",
                ("0x" + f"{i:02x}" * 20, KOL, w1),
            )
        assert rewards._floor_status_for(cur, [KOL], set())[KOL] == "diamond", "twenty-five"


def test_a_referee_qualifies_once_for_whoever_carried_them_over_the_line(_rw):
    rewards = _rw
    w1, w2, w3 = _week(1), _week(2), _week(3)
    storage.upsert_referral_binding(C, A, 1, 0, WEEK1 - 10)

    # half way there under A
    _points(w1, C, 5_000)
    _unblock(rewards, rewards.bucket_end(w1))
    rewards.close_due_weeks(now_ts=rewards.bucket_end(w1) + 60)
    with storage.db_cursor() as cur:
        cur.execute("SELECT referrer, points FROM crystal_rewards_referral_progress WHERE referee = %s", (C,))
        assert [(r, float(p)) for r, p in cur.fetchall()] == [(A, 5000.0)]
        cur.execute("SELECT count(*) FROM crystal_rewards_referral_quals")
        assert cur.fetchone()[0] == 0

    # C switches to B: the counter restarts, so A's five thousand is not inherited
    storage.upsert_referral_binding(C, B, 2, 0, WEEK1 + 10)
    _points(w2, C, 5_000)
    _unblock(rewards, rewards.bucket_end(w2))
    rewards.close_due_weeks(now_ts=rewards.bucket_end(w2) + 60)
    with storage.db_cursor() as cur:
        cur.execute("SELECT referrer, points FROM crystal_rewards_referral_progress WHERE referee = %s", (C,))
        assert [(r, float(p)) for r, p in cur.fetchall()] == [(B, 5000.0)], "restarted under B"
        cur.execute("SELECT count(*) FROM crystal_rewards_referral_quals")
        assert cur.fetchone()[0] == 0, "nobody has carried C ten thousand yet"

    # B finishes the job
    _points(w3, C, 5_000)
    _unblock(rewards, rewards.bucket_end(w3))
    rewards.close_due_weeks(now_ts=rewards.bucket_end(w3) + 60)
    with storage.db_cursor() as cur:
        cur.execute("SELECT referrer FROM crystal_rewards_referral_quals WHERE referee = %s", (C,))
        assert [r for (r,) in cur.fetchall()] == [B]


def test_a_referee_can_never_qualify_a_second_referrer(_rw):
    rewards = _rw
    w1, w2 = _week(1), _week(2)
    storage.upsert_referral_binding(C, A, 1, 0, WEEK1 - 10)
    _points(w1, C, 10_000)
    _unblock(rewards, rewards.bucket_end(w1))
    rewards.close_due_weeks(now_ts=rewards.bucket_end(w1) + 60)

    storage.upsert_referral_binding(C, B, 2, 0, WEEK1 + 10)
    _points(w2, C, 50_000)
    _unblock(rewards, rewards.bucket_end(w2))
    rewards.close_due_weeks(now_ts=rewards.bucket_end(w2) + 60)

    with storage.db_cursor() as cur:
        cur.execute("SELECT referrer FROM crystal_rewards_referral_quals WHERE referee = %s", (C,))
        rows = [r for (r,) in cur.fetchall()]
    assert rows == [A], "C is spent; earning again under B gives B nothing"
