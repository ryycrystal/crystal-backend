"""crystal rewards engine semantics against a real database.

covered invariants: per-activity earning rates, graduated-vs-pregraduation split,
maker and taker stable rates, self-cross exclusion, hourly vault accrual with
campaign multipliers, watermark idempotency, week close math (power curve,
shares, competition ranks, percentile statuses with ties taking the higher
badge), permanence of a finalized week, and referral milestone settlement with
the welcome bonus.
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

USDC = "0x754704bc059f8c67012fed69bc8a327a5aafb603"
AUSD = "0x00000000efe302beaa2b3e6e1b18d08d69a9012a"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

TOK_A = "0x" + "aa" * 20
TOK_B = "0x" + "bb" * 20
VAULT = "0x" + "cc" * 20
U1 = "0x" + "11" * 20
U2 = "0x" + "22" * 20
U3 = "0x" + "33" * 20
U4 = "0x" + "44" * 20

MKT_VOL = "0x" + "e1" * 20
MKT_STABLE = "0x" + "e2" * 20

WEEK1 = int(datetime(2026, 9, 16, 0, 0, tzinfo=LA).timestamp())

REWARDS_TABLES = (
    "crystal_rewards_contrib",
    "crystal_rewards_weeks",
    "crystal_rewards_distributions",
    "crystal_rewards_balances",
    "crystal_rewards_grants",
    "crystal_rewards_milestones",
    "crystal_rewards_campaigns",
    "crystal_rewards_denylist",
    "crystal_rewards_vault_gaps",
    "crystal_market_trades",
    "crystal_orderbook_fills",
    "crystal_vault_deposits",
    "crystal_vault_withdrawals",
    "crystal_vault_balance_samples",
    "referral_bindings",
)


@pytest.fixture(autouse=True)
def _clean_rewards(db, clean):
    import core.rewards as rewards

    with storage.db_cursor() as cur:
        for t in REWARDS_TABLES:
            cur.execute(f"DELETE FROM {t}")
        cur.execute("DELETE FROM crystal_rewards_predeposit_vaults")
        cur.execute("DELETE FROM launchpad_kv WHERE key LIKE 'rewards_%'")
        cur.execute("DELETE FROM crystal_markets WHERE market IN (%s, %s)", (MKT_VOL, MKT_STABLE))
        storage.ensure_rewards_tables(cur=cur)
    storage.set_meta("rewards_program_start", str(WEEK1))
    storage.set_meta("rewards_vault_start", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "1")
    yield rewards


def _seed_token(token: str, migrated_at: int | None) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO launchpad_tokens (token, creator, name, symbol, source, created_block, created_at,
                                          migrated, migrated_block, migrated_at)
            VALUES (%s, %s, 'T', 'T', 0, 1, %s, %s, %s, %s)
            ON CONFLICT (token) DO UPDATE SET migrated = EXCLUDED.migrated, migrated_at = EXCLUDED.migrated_at
            """,
            (token, U4, WEEK1 - 1000, migrated_at is not None, 1 if migrated_at else None, migrated_at),
        )


def _seed_launchpad_trade(idx: int, token: str, user: str, ts: int, usd: float) -> None:
    storage.insert_trade(
        block_number=idx,
        log_index=0,
        timestamp=ts,
        token=token,
        user_address=user,
        is_buy=True,
        native_amount=10**18,
        token_amount=10**18,
        usd_amount=Decimal(str(usd)),
        price_native=Decimal(1),
        txhash=f"0xlp{idx:04d}",
    )


def _seed_market(market: str, quote: str, base: str, qdec: int = 6) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO crystal_markets (market, is_canonical, quote_asset, base_asset,
                quote_address, quote_decimals, quote_ticker, quote_name,
                base_address, base_decimals, base_ticker, base_name)
            VALUES (%s, TRUE, 'Q', 'B', %s, %s, 'Q', 'Q', %s, 18, 'B', 'B')
            ON CONFLICT (market) DO NOTHING
            """,
            (market, quote, qdec, base),
        )


def _seed_taker(tx: str, ts: int, market: str, user: str, quote_amt: int, is_buy: bool = True) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO crystal_market_trades
                (txhash, log_index, block_number, timestamp, market, user_address,
                 is_buy, amount_in, amount_out, start_price, end_price)
            VALUES (%s, 0, 1, %s, %s, %s, %s, %s, %s, 0, 0)
            """,
            (tx, ts, market, user, is_buy, quote_amt if is_buy else 10**18, 10**18 if is_buy else quote_amt),
        )


def _seed_fill(tx: str, li: int, ts: int, market: str, maker: str, quote_amt: int, maker_is_buy: bool = False) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO crystal_orderbook_fills
                (txhash, log_index, block_number, timestamp, market, maker,
                 maker_is_buy, price, order_id, remaining, amount_high, amount_out)
            VALUES (%s, %s, 1, %s, %s, %s, %s, 0, 1, 0, %s, %s)
            """,
            (
                tx,
                li,
                ts,
                market,
                maker,
                maker_is_buy,
                quote_amt if not maker_is_buy else 10**18,
                quote_amt if maker_is_buy else 10**18,
            ),
        )


def _contrib(wallet: str, week: int = WEEK1) -> dict | None:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT pregrad_usd, grad_usd, spot_taker_usd, spot_maker_usd,
                   stable_taker_usd, stable_maker_usd, vault_usd_hours, points
            FROM crystal_rewards_contrib WHERE wallet = %s AND week_start = %s
            """,
            (wallet, week),
        )
        row = cur.fetchone()
    if not row:
        return None
    keys = ("pregrad", "grad", "spot_taker", "spot_maker", "stable_taker", "stable_maker", "vault", "points")
    return {k: float(v) for k, v in zip(keys, row)}


def test_week_boundaries_and_dst(_clean_rewards):
    rewards = _clean_rewards
    assert rewards.week_start_for(WEEK1) == WEEK1
    assert rewards.week_start_for(WEEK1 + 3600) == WEEK1
    assert rewards.week_end_for(WEEK1) - WEEK1 == 7 * 86400
    dst_week = int(datetime(2026, 3, 4, 0, 0, tzinfo=LA).timestamp())
    assert rewards.week_start_for(dst_week + 1000) == dst_week
    assert rewards.week_end_for(dst_week) - dst_week == 7 * 86400 - 3600
    assert rewards.week_start_for(rewards.week_end_for(WEEK1)) == rewards.week_end_for(WEEK1)


def test_launchpad_rates_and_idempotency(_clean_rewards):
    rewards = _clean_rewards
    _seed_token(TOK_A, None)
    _seed_token(TOK_B, WEEK1 + 100)
    _seed_launchpad_trade(1, TOK_A, U1, WEEK1 - 50, 999.0)
    _seed_launchpad_trade(2, TOK_A, U1, WEEK1 + 10, 100.0)
    _seed_launchpad_trade(3, TOK_B, U1, WEEK1 + 200, 100.0)
    _seed_launchpad_trade(4, TOK_B, U2, WEEK1 + 50, 40.0)
    rewards.accrue_launchpad()
    c1 = _contrib(U1)
    assert c1["pregrad"] == pytest.approx(100.0)
    assert c1["grad"] == pytest.approx(100.0)
    assert c1["points"] == pytest.approx(100.0 * 1.0 + 100.0 * 0.10)
    c2 = _contrib(U2)
    assert c2["pregrad"] == pytest.approx(40.0)
    assert c2["points"] == pytest.approx(40.0)
    rewards.accrue_launchpad()
    assert _contrib(U1)["points"] == pytest.approx(110.0)


def test_spot_rates_and_self_cross(_clean_rewards):
    rewards = _clean_rewards
    _seed_market(MKT_VOL, USDC, WMON)
    _seed_market(MKT_STABLE, USDC, AUSD)
    ts = WEEK1 + 100
    _seed_taker("0xt1", ts, MKT_VOL, U1, 100_000_000)
    _seed_fill("0xt1", 1, ts, MKT_VOL, U3, 100_000_000, maker_is_buy=False)
    _seed_taker("0xt2", ts, MKT_STABLE, U1, 100_000_000)
    _seed_fill("0xt2", 1, ts, MKT_STABLE, U3, 100_000_000, maker_is_buy=False)
    _seed_taker("0xt3", ts, MKT_VOL, U2, 50_000_000)
    _seed_fill("0xt3", 1, ts, MKT_VOL, U2, 50_000_000, maker_is_buy=False)
    rewards.accrue_spot_takers()
    rewards.accrue_spot_makers()
    c1 = _contrib(U1)
    assert c1["spot_taker"] == pytest.approx(100.0)
    assert c1["stable_taker"] == pytest.approx(100.0)
    assert c1["points"] == pytest.approx(100.0 * 0.05 + 100.0 * 0.01)
    c3 = _contrib(U3)
    assert c3["spot_maker"] == pytest.approx(100.0)
    assert c3["stable_maker"] == pytest.approx(100.0)
    assert c3["points"] == pytest.approx(100.0 * 0.01 + 100.0 * 0.002)
    assert _contrib(U2) is None
    rewards.accrue_spot_takers()
    rewards.accrue_spot_makers()
    assert _contrib(U1)["points"] == pytest.approx(6.0)


def test_vault_hourly_accrual_and_campaign(_clean_rewards):
    rewards = _clean_rewards
    hour1 = WEEK1 + 3600
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (1, 0, %s, %s, %s, 100, 0, 0, '0xd1'), (2, 0, %s, %s, %s, 400, 0, 0, '0xd2')",
            (WEEK1, VAULT, U1, WEEK1 + 60, VAULT, U2),
        )
        cur.execute(
            "INSERT INTO crystal_vault_withdrawals (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (3, 0, %s, %s, %s, 100, 0, 0, '0xw1')",
            (WEEK1 + 120, VAULT, U2),
        )
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance, base_balance, usd_value) "
            "VALUES (%s, 10, %s, 0, 0, 4000)",
            (VAULT, WEEK1 + 1800),
        )
        cur.execute(
            "INSERT INTO crystal_rewards_campaigns (vault, multiplier, start_ts, end_ts) VALUES (%s, 3.0, %s, %s)",
            (VAULT, hour1 + 3600, hour1 + 7200),
        )
    done = rewards.accrue_vaults(now_ts=hour1 + 10)
    assert done == 1
    c1 = _contrib(U1)
    c2 = _contrib(U2)
    assert c1["vault"] == pytest.approx(1000.0)
    assert c1["points"] == pytest.approx(50.0)
    assert c2["vault"] == pytest.approx(3000.0)
    assert c2["points"] == pytest.approx(150.0)
    done = rewards.accrue_vaults(now_ts=hour1 + 3600 + 10)
    assert done == 1
    assert _contrib(U1)["points"] == pytest.approx(50.0 + 150.0)
    assert _contrib(U2)["points"] == pytest.approx(150.0 + 450.0)
    assert rewards.accrue_vaults(now_ts=hour1 + 3600 + 10) == 0


def test_status_ladder(_clean_rewards):
    rewards = _clean_rewards
    assert rewards._status_for(1, 100) == "diamond"
    assert rewards._status_for(2, 100) == "platinum"
    assert rewards._status_for(10, 100) == "platinum"
    assert rewards._status_for(11, 100) == "gold"
    assert rewards._status_for(25, 100) == "gold"
    assert rewards._status_for(26, 100) == "silver"
    assert rewards._status_for(50, 100) == "silver"
    assert rewards._status_for(51, 100) == "bronze"
    assert rewards._status_for(1, 3) == "diamond"
    assert rewards._status_for(2, 3) == "silver"
    assert rewards._status_for(3, 3) == "bronze"


def test_week_close_distribution_and_milestones(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    storage.set_meta("rewards_pool", "10000")
    storage.set_meta("rewards_milestones", "[[1000, 111, 222]]")
    now = week_end + 100
    with storage.db_cursor() as cur:
        storage.add_rewards_contrib(cur, WEEK1, U1, now, points=10_000)
        storage.add_rewards_contrib(cur, WEEK1, U2, now, points=100)
        storage.add_rewards_contrib(cur, WEEK1, U3, now, points=100)
        storage.add_rewards_contrib(cur, WEEK1, U4, now, points=1)
    storage.upsert_referral_binding(U1, U4, 1, 0, WEEK1)
    storage.upsert_referral_binding(U2, U1, 1, 0, WEEK1)

    assert rewards.close_due_weeks(now_ts=now) == []

    storage.set_meta("rewards_wm_vault_hour", str(week_end))
    _seed_token(TOK_A, None)
    _seed_launchpad_trade(99, TOK_A, "0x" + "99" * 20, week_end + 5, 0.0)
    storage.set_meta("rewards_wm_launchpad", "999999")

    closed = rewards.close_due_weeks(now_ts=now)
    assert closed == [WEEK1]

    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT wallet, crystals, rank, status FROM crystal_rewards_distributions WHERE week_start = %s ORDER BY rank, wallet",
            (WEEK1,),
        )
        dist = {w: (float(c), int(r), s) for w, c, r, s in cur.fetchall()}

    adj = {U1: 10_000**0.8, U2: 100**0.8, U3: 100**0.8, U4: 1.0}
    total_adj = sum(adj.values())
    assert dist[U1][0] == pytest.approx(10000 * adj[U1] / total_adj, rel=1e-6)
    assert dist[U2][0] == pytest.approx(10000 * adj[U2] / total_adj, rel=1e-6)
    assert dist[U1][1:] == (1, "diamond")
    assert dist[U2][1:] == (2, "silver")
    assert dist[U3][1:] == (2, "silver")
    assert dist[U4][1:] == (4, "bronze")

    with storage.db_cursor() as cur:
        cur.execute("SELECT wallet, kind, amount, ref FROM crystal_rewards_grants ORDER BY id")
        grants = [(w, k, float(a), r) for w, k, a, r in cur.fetchall()]
    assert (U4, "referral", 111.0, U1) in grants
    assert (U1, "welcome", 222.0, U4) in grants
    assert len(grants) == 2

    with storage.db_cursor() as cur:
        bal_u1 = storage.get_rewards_balance(cur, U1)
        bal_u4 = storage.get_rewards_balance(cur, U4)
    assert bal_u1 == pytest.approx(dist[U1][0] + 222.0, rel=1e-6)
    assert bal_u4 == pytest.approx(dist[U4][0] + 111.0, rel=1e-6)

    assert rewards.close_due_weeks(now_ts=now) == []
    with storage.db_cursor() as cur:
        assert storage.get_rewards_balance(cur, U1) == pytest.approx(bal_u1, rel=1e-9)


def test_denylist_excluded_from_close(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    with storage.db_cursor() as cur:
        storage.add_rewards_contrib(cur, WEEK1, U1, now, points=500)
        storage.add_rewards_contrib(cur, WEEK1, U2, now, points=500)
        cur.execute("INSERT INTO crystal_rewards_denylist (wallet) VALUES (%s)", (U2,))
    storage.set_meta("rewards_wm_vault_hour", str(week_end))
    _seed_token(TOK_A, None)
    _seed_launchpad_trade(98, TOK_A, "0x" + "98" * 20, week_end + 5, 0.0)
    storage.set_meta("rewards_wm_launchpad", "999999")
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1]
    with storage.db_cursor() as cur:
        cur.execute("SELECT wallet FROM crystal_rewards_distributions WHERE week_start = %s", (WEEK1,))
        wallets = {r[0] for r in cur.fetchall()}
    assert wallets == {U1}
    with storage.db_cursor() as cur:
        assert storage.get_rewards_balance(cur, U1) == pytest.approx(1_000_000.0, rel=1e-6)


def test_vault_predeposit_boost(_clean_rewards):
    rewards = _clean_rewards
    vault_start = WEEK1 - 7200
    storage.set_meta("rewards_vault_start", str(vault_start))
    storage.set_meta("rewards_predeposit_cutoff", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "3")
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (1, 0, %s, %s, %s, 100, 0, 0, '0xp1'), (2, 0, %s, %s, %s, 100, 0, 0, '0xp2')",
            (vault_start, VAULT, U1, WEEK1 + 10, VAULT, U1),
        )
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance, base_balance, usd_value) "
            "VALUES (%s, 10, %s, 0, 0, 4000)",
            (VAULT, vault_start),
        )
    assert rewards.accrue_vaults(now_ts=WEEK1 - 3600 + 10) == 1
    assert _contrib(U1)["points"] == pytest.approx(600.0)
    assert _contrib(U1)["vault"] == pytest.approx(4000.0)
    assert rewards.accrue_vaults(now_ts=WEEK1 + 10) == 1
    assert _contrib(U1)["points"] == pytest.approx(1200.0)
    assert rewards.accrue_vaults(now_ts=WEEK1 + 3610) == 1
    assert _contrib(U1)["points"] == pytest.approx(1200.0 + 400.0)
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_withdrawals (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (3, 0, %s, %s, %s, 150, 0, 0, '0xpw')",
            (WEEK1 + 3700, VAULT, U1),
        )
    assert rewards.accrue_vaults(now_ts=WEEK1 + 7210) == 1
    assert _contrib(U1)["points"] == pytest.approx(1600.0 + 600.0)
    assert _contrib(U1)["vault"] == pytest.approx(16000.0)
    with storage.db_cursor() as cur:
        cur.execute("SELECT DISTINCT week_start FROM crystal_rewards_contrib WHERE wallet = %s", (U1,))
        assert {int(r[0]) for r in cur.fetchall()} == {WEEK1}


def test_bucket_clamps_premain_activity_into_week_one(_clean_rewards):
    rewards = _clean_rewards
    assert rewards.bucket_for(WEEK1 - 86400, WEEK1) == WEEK1
    assert rewards.bucket_for(WEEK1 + 86400, WEEK1) == WEEK1
    assert rewards.bucket_for(rewards.week_end_for(WEEK1) + 10, WEEK1) == rewards.week_end_for(WEEK1)
    assert rewards.bucket_end(WEEK1) == rewards.week_end_for(WEEK1)


def test_launch_timeline_end_to_end(_clean_rewards):
    rewards = _clean_rewards
    vault_start = int(datetime(2026, 9, 8, 7, 0, tzinfo=LA).timestamp())
    week1_end = rewards.week_end_for(WEEK1)
    week2_end = rewards.week_end_for(week1_end)
    storage.set_meta("rewards_vault_start", str(vault_start))
    storage.set_meta("rewards_predeposit_cutoff", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "3")

    lp1, lp2, t1, t2, r1 = U1, U2, U3, U4, "0x" + "55" * 20
    d1 = vault_start + 3600 + 1
    d2 = WEEK1 + 86400 + 1
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (1, 0, %s, %s, %s, 100, 0, 0, '0xl1'), (2, 0, %s, %s, %s, 100, 0, 0, '0xl2')",
            (d1, VAULT, lp1, d2, VAULT, lp2),
        )
        for k in range(23):
            cur.execute(
                "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance, base_balance, usd_value) "
                "VALUES (%s, %s, %s, 0, 0, 2400)",
                (VAULT, 100 + k, vault_start + k * 86400),
            )

    _seed_token(TOK_A, None)
    _seed_launchpad_trade(11, TOK_A, t1, WEEK1 + 12 * 3600, 1000.0)
    _seed_market(MKT_VOL, USDC, WMON)
    _seed_taker("0xw2", week1_end + 86400, MKT_VOL, t2, 200_000_000)
    _seed_launchpad_trade(12, TOK_A, t1, week2_end + 5, 0.0)
    storage.upsert_referral_binding(t1, r1, 1, 0, WEEK1)

    now = week2_end + 100
    rewards.accrue_launchpad()
    rewards.accrue_spot_takers()
    rewards.accrue_spot_makers()
    while rewards.accrue_vaults(now_ts=now) > 0:
        pass

    first_hour = ((d1 // 3600) + 1) * 3600
    lp2_hour = ((d2 // 3600) + 1) * 3600
    solo_hours = (d2 // 3600) - (first_hour // 3600) + 1
    shared_w1 = (week1_end - lp2_hour) // 3600 + 1
    shared_w2 = (week2_end - week1_end) // 3600

    c_lp1 = _contrib(lp1)
    c_lp2 = _contrib(lp2)
    assert c_lp1["vault"] == pytest.approx(solo_hours * 2400 + shared_w1 * 1200, rel=1e-9)
    assert c_lp1["points"] == pytest.approx(solo_hours * 360.0 + shared_w1 * 180.0, rel=1e-9)
    assert c_lp2["vault"] == pytest.approx(shared_w1 * 1200, rel=1e-9)
    assert c_lp2["points"] == pytest.approx(shared_w1 * 60.0, rel=1e-9)
    c_lp1_w2 = _contrib(lp1, week1_end)
    assert c_lp1_w2["points"] == pytest.approx(shared_w2 * 180.0, rel=1e-9)
    assert _contrib(t1)["points"] == pytest.approx(1000.0)
    assert _contrib(t2, week1_end)["points"] == pytest.approx(10.0)

    closed = rewards.close_due_weeks(now_ts=now)
    assert closed == [WEEK1, week1_end]

    import math as _m

    pts = {
        lp1: c_lp1["points"],
        lp2: c_lp2["points"],
        t1: 1000.0,
    }
    adj = {w: _m.pow(p, 0.8) for w, p in pts.items()}
    total_adj = sum(adj.values())
    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT wallet, crystals, rank, participants, status FROM crystal_rewards_distributions WHERE week_start = %s",
            (WEEK1,),
        )
        w1 = {w: (float(c), int(rk), int(n), s) for w, c, rk, n, s in cur.fetchall()}
        cur.execute(
            "SELECT wallet, crystals, rank, participants, status FROM crystal_rewards_distributions WHERE week_start = %s",
            (week1_end,),
        )
        w2 = {w: (float(c), int(rk), int(n), s) for w, c, rk, n, s in cur.fetchall()}

    assert set(w1) == {lp1, lp2, t1}
    for w in (lp1, lp2, t1):
        assert w1[w][0] == pytest.approx(1_000_000 * adj[w] / total_adj, rel=1e-6)
    assert w1[lp1][1:] == (1, 3, "diamond")
    order = sorted(pts, key=pts.get, reverse=True)
    assert order[0] == lp1

    assert set(w2) == {lp1, lp2, t2}
    total_w2 = sum(v[0] for v in w2.values())
    assert total_w2 == pytest.approx(1_000_000, rel=1e-6)

    with storage.db_cursor() as cur:
        cur.execute("SELECT wallet, kind, amount, ref FROM crystal_rewards_grants ORDER BY id")
        grants = [(w, k, float(a), r) for w, k, a, r in cur.fetchall()]
        t1_crystals = w1[t1][0]
        bal_r1 = storage.get_rewards_balance(cur, r1)
    expected_referrer = sum(m[1] for m in rewards.milestones() if m[0] <= t1_crystals)
    expected_welcome = sum(m[2] for m in rewards.milestones() if m[0] <= t1_crystals)
    assert bal_r1 == pytest.approx(expected_referrer, rel=1e-9)
    assert sum(a for w, k, a, _r in grants if w == t1 and k == "welcome") == pytest.approx(expected_welcome, rel=1e-9)

    assert rewards.close_due_weeks(now_ts=now) == []
    assert rewards.accrue_vaults(now_ts=now) == 0


def test_double_close_cannot_double_pay(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    with storage.db_cursor() as cur:
        storage.add_rewards_contrib(cur, WEEK1, U1, now, points=500)
    storage.set_meta("rewards_wm_vault_hour", str(week_end))
    _seed_token(TOK_A, None)
    _seed_launchpad_trade(97, TOK_A, "0x" + "97" * 20, week_end + 5, 0.0)
    storage.set_meta("rewards_wm_launchpad", "999999")
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1]
    with storage.db_cursor() as cur:
        bal = storage.get_rewards_balance(cur, U1)
        cur.execute("UPDATE crystal_rewards_weeks SET finalized = FALSE WHERE week_start = %s", (WEEK1,))
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1]
    with storage.db_cursor() as cur:
        assert storage.get_rewards_balance(cur, U1) == pytest.approx(bal, rel=1e-12)
        cur.execute("SELECT COUNT(*) FROM crystal_rewards_grants")
        assert int(cur.fetchone()[0]) == 0


def test_leadership_guard_stops_accrual(_clean_rewards):
    rewards = _clean_rewards
    _seed_token(TOK_A, None)
    _seed_launchpad_trade(96, TOK_A, U1, WEEK1 + 10, 100.0)
    assert rewards.accrue_launchpad(guard=lambda: False) == 0
    assert _contrib(U1) is None
    assert rewards.accrue_launchpad(guard=lambda: True) > 0
    assert _contrib(U1)["points"] == pytest.approx(100.0)


def test_leader_claim_exclusivity(_clean_rewards):
    assert storage.claim_rewards_leader("node-a", 300)
    assert not storage.claim_rewards_leader("node-b", 300)
    assert storage.claim_rewards_leader("node-a", 300)
    with storage.db_cursor() as cur:
        cur.execute("UPDATE crystal_rewards_leader SET heartbeat_at = Now() - interval '10 minutes' WHERE id = 1")
    assert storage.claim_rewards_leader("node-b", 300)


def test_predeposit_boost_burns_on_round_trip(_clean_rewards):
    rewards = _clean_rewards
    vault_start = WEEK1 - 7200
    storage.set_meta("rewards_vault_start", str(vault_start))
    storage.set_meta("rewards_predeposit_cutoff", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "3")
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (1, 0, %s, %s, %s, 100, 0, 0, '0xb1'), (3, 0, %s, %s, %s, 100, 0, 0, '0xb2')",
            (vault_start, VAULT, U1, WEEK1 + 200, VAULT, U1),
        )
        cur.execute(
            "INSERT INTO crystal_vault_withdrawals (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (2, 0, %s, %s, %s, 100, 0, 0, '0xbw')",
            (WEEK1 + 100, VAULT, U1),
        )
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance, base_balance, usd_value) "
            "VALUES (%s, 10, %s, 0, 0, 4000)",
            (VAULT, vault_start),
        )
    while rewards.accrue_vaults(now_ts=WEEK1 + 3610) > 0:
        pass
    c = _contrib(U1)
    assert c["vault"] == pytest.approx(4000.0 * 3)
    assert c["points"] == pytest.approx(600.0 * 2 + 200.0)


def test_concurrent_accrual_cannot_double_count(_clean_rewards):
    import threading

    rewards = _clean_rewards
    _seed_token(TOK_A, None)
    for i in range(60):
        _seed_launchpad_trade(500 + i, TOK_A, U1, WEEK1 + 10 + i, 10.0)

    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()
            rewards.accrue_launchpad()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert _contrib(U1)["points"] == pytest.approx(600.0)
    assert _contrib(U1)["pregrad"] == pytest.approx(600.0)


def test_config_endpoints_manage_denylist_and_bonus_vaults(_clean_rewards):
    addr = "0x" + "7c" * 20
    vault = "0x" + "9d" * 20

    assert storage.add_rewards_denylist([addr.upper()]) == [addr]
    assert storage.list_rewards_denylist() == [addr]
    assert storage.add_rewards_denylist([addr]) == [addr]
    assert storage.list_rewards_denylist() == [addr]

    assert storage.add_predeposit_vaults([vault.upper()]) == [vault]
    assert storage.list_predeposit_vaults() == [vault]

    with storage.db_cursor() as cur:
        assert storage.rewards_denylist(cur) == {addr}
        assert storage.rewards_predeposit_vaults(cur) == {vault}

    assert storage.remove_rewards_denylist(addr) is True
    assert storage.remove_rewards_denylist(addr) is False
    assert storage.list_rewards_denylist() == []
    assert storage.remove_predeposit_vault(vault) is True
    assert storage.list_predeposit_vaults() == []


def test_denylisted_wallet_never_accrues(_clean_rewards):
    rewards = _clean_rewards
    storage.add_rewards_denylist([U2])
    _seed_token(TOK_A, None)
    _seed_launchpad_trade(700, TOK_A, U1, WEEK1 + 10, 50.0)
    _seed_launchpad_trade(701, TOK_A, U2, WEEK1 + 20, 5000.0)
    rewards.accrue_launchpad()
    assert _contrib(U1)["points"] == pytest.approx(50.0)
    assert _contrib(U2) is None


def test_bonus_vault_scoping_limits_the_boost(_clean_rewards):
    rewards = _clean_rewards
    other = "0x" + "ce" * 20
    vault_start = WEEK1 - 7200
    storage.set_meta("rewards_vault_start", str(vault_start))
    storage.set_meta("rewards_predeposit_cutoff", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "3")
    storage.add_predeposit_vaults([VAULT])
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (1, 0, %s, %s, %s, 100, 0, 0, '0xs1'), (2, 0, %s, %s, %s, 100, 0, 0, '0xs2')",
            (vault_start, VAULT, U1, vault_start, other, U2),
        )
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance, base_balance, usd_value) "
            "VALUES (%s, 10, %s, 0, 0, 1000), (%s, 11, %s, 0, 0, 1000)",
            (VAULT, vault_start, other, vault_start),
        )
    assert rewards.accrue_vaults(now_ts=vault_start + 3610) == 1
    assert _contrib(U1)["points"] == pytest.approx(150.0)
    assert _contrib(U2)["points"] == pytest.approx(50.0)


def test_boost_only_covers_deposits_inside_the_window(_clean_rewards):
    rewards = _clean_rewards
    pd_start = WEEK1 - 8 * 86400
    storage.set_meta("rewards_vault_start", str(pd_start))
    storage.set_meta("rewards_predeposit_start", str(pd_start))
    storage.set_meta("rewards_predeposit_cutoff", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "3")

    early, inwin, late = U1, U2, U3
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address,"
            " shares, quote_amount, base_amount, txhash) VALUES"
            " (1, 0, %s, %s, %s, 100, 0, 0, '0xw1'),"
            " (2, 0, %s, %s, %s, 100, 0, 0, '0xw2'),"
            " (3, 0, %s, %s, %s, 100, 0, 0, '0xw3')",
            (pd_start - 86400, VAULT, early, pd_start + 3600, VAULT, inwin, WEEK1 + 3600, VAULT, late),
        )
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance,"
            " base_balance, usd_value) VALUES (%s, 5, %s, 0, 0, 300)",
            (VAULT, WEEK1 + 3600),
        )

    hour = WEEK1 + 7200
    storage.set_meta("rewards_wm_vault_hour", str(hour - 3600))
    assert rewards.accrue_vaults(now_ts=hour + 10) == 1

    def rate(w):
        c = _contrib(w)
        return c["points"] / c["vault"]

    assert rate(early) == pytest.approx(0.05), "a deposit before the window opened must earn 1x"
    assert rate(inwin) == pytest.approx(0.15), "a deposit inside the window must earn 3x"
    assert rate(late) == pytest.approx(0.05), "a deposit after the cutoff must earn 1x"


def test_boost_survives_trimming_but_burns_below_the_predeposit(_clean_rewards):
    rewards = _clean_rewards
    pd_start = WEEK1 - 8 * 86400
    storage.set_meta("rewards_vault_start", str(pd_start))
    storage.set_meta("rewards_predeposit_start", str(pd_start))
    storage.set_meta("rewards_predeposit_cutoff", str(WEEK1))
    storage.set_meta("rewards_predeposit_multiplier", "3")

    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, user_address,"
            " shares, quote_amount, base_amount, txhash) VALUES"
            " (1, 0, %s, %s, %s, 100, 0, 0, '0xb1'),"
            " (2, 0, %s, %s, %s, 100, 0, 0, '0xb2')",
            (pd_start + 60, VAULT, U1, WEEK1 + 60, VAULT, U1),
        )
        cur.execute(
            "INSERT INTO crystal_vault_withdrawals (block_number, log_index, timestamp, vault, user_address,"
            " shares, quote_amount, base_amount, txhash) VALUES (3, 0, %s, %s, %s, 150, 0, 0, '0xbw')",
            (WEEK1 + 120, VAULT, U1),
        )
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance,"
            " base_balance, usd_value) VALUES (%s, 5, %s, 0, 0, 50)",
            (VAULT, WEEK1 + 3600),
        )

    hour = WEEK1 + 7200
    storage.set_meta("rewards_wm_vault_hour", str(hour - 3600))
    assert rewards.accrue_vaults(now_ts=hour + 10) == 1
    c = _contrib(U1)
    # 100 boosted + 100 plain, withdrew 150: the plain 100 goes first, then 50 of
    # the boost is burned, leaving 50 boosted shares of a 50 share position
    assert c["vault"] == pytest.approx(50.0)
    assert c["points"] / c["vault"] == pytest.approx(0.15)


def _deposit(idx: int, user: str, shares: int, ts: int, vault: str = VAULT) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_deposits (block_number, log_index, timestamp, vault, "
            "user_address, shares, quote_amount, base_amount, txhash) "
            "VALUES (%s, 0, %s, %s, %s, %s, 0, 0, %s)",
            (idx, ts, vault, user, shares, f"0xg{idx:04d}"),
        )


def _sample(ts: int, usd: float, vault: str = VAULT, block: int = 10) -> None:
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, "
            "quote_balance, base_balance, usd_value) VALUES (%s, %s, %s, 0, 0, %s)",
            (vault, block, ts, usd),
        )


def _gaps(week: int | None = None) -> list[dict]:
    with storage.db_cursor() as cur:
        return storage.vault_gaps(cur, week)


def test_vault_hour_with_no_sample_is_recorded_not_silently_skipped(_clean_rewards, capsys):
    rewards = _clean_rewards
    hour1 = WEEK1 + 3600
    _deposit(1, U1, 100, WEEK1)
    assert rewards.accrue_vaults(now_ts=hour1 + 10) == 1
    assert _contrib(U1) is None, "an unvalued vault must not pay out"
    gaps = _gaps()
    assert len(gaps) == 1
    assert gaps[0]["vault"] == VAULT
    assert gaps[0]["reason"] == "no_sample"
    assert gaps[0]["holders"] == 1
    assert gaps[0]["hours"] == 1
    assert "unvalued" in capsys.readouterr().out, "the outage must be loud"


def test_stale_and_zero_samples_are_distinguished(_clean_rewards):
    rewards = _clean_rewards
    hour1 = WEEK1 + 3600
    _deposit(1, U1, 100, WEEK1)
    _sample(hour1 - rewards.VAULT_SAMPLE_STALENESS - 60, 4000.0)
    assert rewards.accrue_vaults(now_ts=hour1 + 10) == 1
    assert _gaps()[0]["reason"] == "stale_sample"

    storage.set_meta("rewards_wm_vault_hour", str(WEEK1))
    _sample(hour1 - 60, 0.0, block=11)
    assert rewards.accrue_vaults(now_ts=hour1 + 10) == 1
    assert _gaps()[0]["reason"] == "zero_value"


def test_backfilled_sample_clears_the_recorded_gap(_clean_rewards):
    rewards = _clean_rewards
    hour1 = WEEK1 + 3600
    _deposit(1, U1, 100, WEEK1)
    assert rewards.accrue_vaults(now_ts=hour1 + 10) == 1
    assert len(_gaps()) == 1

    storage.set_meta("rewards_wm_vault_hour", str(WEEK1))
    _sample(hour1 - 60, 4000.0)
    assert rewards.accrue_vaults(now_ts=hour1 + 10) == 1
    assert _gaps() == [], "a backfilled sample must clear the gap it caused"
    assert _contrib(U1)["points"] == pytest.approx(200.0)


def _clean_close_sources(now: int) -> int:
    import core.rewards as rewards

    week_end = rewards.week_end_for(WEEK1)
    with storage.db_cursor() as cur:
        storage.add_rewards_contrib(cur, WEEK1, U1, now, points=500)
    storage.set_meta("rewards_wm_vault_hour", str(week_end))
    _seed_token(TOK_A, None)
    _seed_launchpad_trade(91, TOK_A, "0x" + "91" * 20, week_end + 5, 0.0)
    storage.set_meta("rewards_wm_launchpad", "999999")
    return week_end


def test_close_refuses_while_a_vault_gap_exceeds_tolerance(_clean_rewards, capsys):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    _clean_close_sources(now)
    tolerance = rewards.gap_tolerance()
    with storage.db_cursor() as cur:
        for i in range(tolerance + 1):
            storage.record_vault_gap(cur, WEEK1 + i * 3600, VAULT, WEEK1, 3, 100, 0, "no_sample")

    assert rewards.close_due_weeks(now_ts=now) == [], "a sampling outage must block the close"
    assert "refusing to close" in capsys.readouterr().out
    with storage.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM crystal_rewards_distributions")
        assert int(cur.fetchone()[0]) == 0
        assert storage.get_rewards_balance(cur, U1) == 0.0


def test_close_proceeds_at_exactly_the_tolerance(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    _clean_close_sources(now)
    with storage.db_cursor() as cur:
        for i in range(rewards.gap_tolerance()):
            storage.record_vault_gap(cur, WEEK1 + i * 3600, VAULT, WEEK1, 3, 100, 0, "no_sample")
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1]


def test_gap_tolerance_counts_per_vault_not_in_total(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    _clean_close_sources(now)
    other = "0x" + "dd" * 20
    with storage.db_cursor() as cur:
        for i in range(rewards.gap_tolerance()):
            storage.record_vault_gap(cur, WEEK1 + i * 3600, VAULT, WEEK1, 3, 100, 0, "no_sample")
            storage.record_vault_gap(cur, WEEK1 + i * 3600, other, WEEK1, 3, 100, 0, "no_sample")
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1], "two tolerable vaults are not one intolerable one"


def test_acknowledging_the_gap_lets_the_week_close(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    _clean_close_sources(now)
    with storage.db_cursor() as cur:
        for i in range(rewards.gap_tolerance() + 5):
            storage.record_vault_gap(cur, WEEK1 + i * 3600, VAULT, WEEK1, 3, 100, 0, "no_sample")
    assert rewards.close_due_weeks(now_ts=now) == []

    storage.set_meta(rewards.gap_ack_key(WEEK1), "1")
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1]
    with storage.db_cursor() as cur:
        assert storage.get_rewards_balance(cur, U1) == pytest.approx(rewards.pool_size(), rel=1e-9)


def test_acknowledging_one_week_does_not_unblock_another(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    _clean_close_sources(now)
    with storage.db_cursor() as cur:
        for i in range(rewards.gap_tolerance() + 1):
            storage.record_vault_gap(cur, WEEK1 + i * 3600, VAULT, WEEK1, 3, 100, 0, "no_sample")
    storage.set_meta(rewards.gap_ack_key(week_end), "1")
    assert rewards.close_due_weeks(now_ts=now) == [], "the ack must be scoped to its own week"


def test_gaps_from_another_week_do_not_block_this_one(_clean_rewards):
    rewards = _clean_rewards
    week_end = rewards.week_end_for(WEEK1)
    now = week_end + 100
    _clean_close_sources(now)
    with storage.db_cursor() as cur:
        for i in range(rewards.gap_tolerance() + 4):
            storage.record_vault_gap(cur, week_end + i * 3600, VAULT, week_end, 3, 100, 0, "no_sample")
    assert rewards.close_due_weeks(now_ts=now) == [WEEK1]
