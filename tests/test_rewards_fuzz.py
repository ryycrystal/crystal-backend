"""randomized property tests for the rewards engine against a real database.

each seed generates a random universe of vaults, depositors, flows, samples,
campaigns, trades and fills, then asserts the engine agrees with an
independent pure-python reference model and that the ledger invariants hold:
boost equals the running minimum of net position since the cutoff, accrual is
idempotent under re-runs and random chunking, weekly distributions conserve
the pool, shares sum to one, ranks are monotone in points, closing twice
changes nothing, and every wallet balance equals its distributions plus
grants.
"""

import os
import random
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
from tests.test_rewards import REWARDS_TABLES  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")
WEEK1 = int(datetime(2026, 9, 16, 0, 0, tzinfo=LA).timestamp())
VAULT_START = int(datetime(2026, 9, 8, 7, 0, tzinfo=LA).timestamp())
PD_MULT = Decimal(3)
VAULT_RATE = Decimal("0.05")
USD_SCALE = Decimal("1")


@pytest.fixture(autouse=True)
def _clean_rewards(db, clean):
    import core.rewards as rewards

    with storage.db_cursor() as cur:
        for t in REWARDS_TABLES:
            cur.execute(f"DELETE FROM {t}")
        cur.execute("DELETE FROM crystal_rewards_predeposit_vaults")
        cur.execute("DELETE FROM launchpad_kv WHERE key LIKE 'rewards_%'")
        storage.ensure_rewards_tables(cur=cur)
    storage.set_meta("rewards_program_start", str(WEEK1))
    storage.set_meta("rewards_vault_start", str(VAULT_START))
    storage.set_meta("rewards_predeposit_multiplier", str(PD_MULT))
    yield rewards


def _addr(rng: random.Random) -> str:
    return "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40))


class Universe:
    def __init__(self, seed: int):
        rng = random.Random(seed)
        self.rng = rng
        self.vaults = [_addr(rng) for _ in range(rng.randint(2, 3))]
        self.users = [_addr(rng) for _ in range(rng.randint(3, 6))]
        self.cutoff = WEEK1
        self.pd_start = VAULT_START
        self.horizon = WEEK1 + rng.randint(12, 30) * 3600
        self.flows: list[tuple[int, str, str, int]] = []
        self.samples: dict[str, list[tuple[int, Decimal]]] = {v: [] for v in self.vaults}
        self.campaigns: list[tuple[str, Decimal, int, int]] = []
        self.pd_vaults: set[str] = set()

        net: dict[tuple[str, str], int] = {}
        t = VAULT_START - rng.randint(0, 6) * 3600
        while t < self.horizon:
            t += rng.randint(300, 14400)
            v = rng.choice(self.vaults)
            u = rng.choice(self.users)
            cur = net.get((v, u), 0)
            if cur > 0 and rng.random() < 0.45:
                amount = rng.randint(1, cur)
                self.flows.append((t, v, u, -amount))
                net[(v, u)] = cur - amount
            else:
                amount = rng.randint(50, 5000)
                self.flows.append((t, v, u, amount))
                net[(v, u)] = cur + amount

        for v in self.vaults:
            st = VAULT_START - 3600
            while st < self.horizon:
                if rng.random() < 0.9:
                    self.samples[v].append((st, Decimal(rng.randint(1_000, 500_000))))
                st += rng.randint(1200, 5400)

        for _ in range(rng.randint(0, 3)):
            v = rng.choice(self.vaults)
            s = VAULT_START + rng.randint(0, 20) * 3600
            self.campaigns.append((v, Decimal(rng.choice(["1.5", "2", "3"])), s, s + rng.randint(1, 12) * 3600))

        if rng.random() < 0.5:
            self.pd_vaults = set(rng.sample(self.vaults, rng.randint(1, len(self.vaults))))

    def seed_db(self) -> None:
        with storage.db_cursor() as cur:
            for i, (ts, v, u, delta) in enumerate(sorted(self.flows)):
                table = "crystal_vault_deposits" if delta > 0 else "crystal_vault_withdrawals"
                cur.execute(
                    f"INSERT INTO {table} (block_number, log_index, timestamp, vault, user_address, shares,"
                    f" quote_amount, base_amount, txhash) VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s)",
                    (i + 1, i % 4, ts, v, u, abs(delta), f"0xf{i:05d}"),
                )
            for v, rows in self.samples.items():
                for j, (ts, usd) in enumerate(rows):
                    cur.execute(
                        "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp,"
                        " quote_balance, base_balance, usd_value) VALUES (%s, %s, %s, 0, 0, %s)",
                        (v, j + 1, ts, usd),
                    )
            for v, mult, s, e in self.campaigns:
                cur.execute(
                    "INSERT INTO crystal_rewards_campaigns (vault, multiplier, start_ts, end_ts) VALUES (%s, %s, %s, %s)",
                    (v, mult, s, e),
                )
            for v in self.pd_vaults:
                cur.execute("INSERT INTO crystal_rewards_predeposit_vaults (vault) VALUES (%s)", (v,))

    def expected_vault_contrib(self) -> dict[str, dict[str, Decimal]]:
        flows = sorted(self.flows)
        first_hour = (VAULT_START // 3600) * 3600 + 3600
        out: dict[str, dict[str, Decimal]] = {}
        hour = first_hour
        while hour <= self.horizon:
            nets: dict[tuple[str, str], int] = {}
            for ts, v, u, delta in flows:
                if ts <= hour:
                    nets[(v, u)] = nets.get((v, u), 0) + delta
            supply: dict[str, Decimal] = {}
            for (v, _u), n in nets.items():
                if n > 0:
                    supply[v] = supply.get(v, Decimal(0)) + Decimal(n)
            for (v, u), n in nets.items():
                if n <= 0:
                    continue
                usd = None
                for sts, susd in sorted(self.samples[v]):
                    if sts <= hour and sts >= hour - 86400:
                        usd = susd
                    elif sts > hour:
                        break
                if usd is None or usd <= 0:
                    continue
                # only deposits inside the announced window earn the boost, and a
                # withdrawal spends unboosted shares before it burns boosted ones
                boosted_bal = 0
                plain_bal = 0
                for ts, fv, fu, delta in flows:
                    if (fv, fu) != (v, u) or ts > hour:
                        continue
                    if delta >= 0:
                        if self.pd_start <= ts <= self.cutoff:
                            boosted_bal += delta
                        else:
                            plain_bal += delta
                    else:
                        owed = -delta
                        spend = min(owed, plain_bal)
                        plain_bal -= spend
                        owed -= spend
                        if owed > 0:
                            boosted_bal = max(0, boosted_bal - owed)
                boosted = Decimal(min(boosted_bal, n))
                if self.pd_vaults and v not in self.pd_vaults:
                    boosted = Decimal(0)
                mult = Decimal(1)
                for cv, cm, cs, ce in self.campaigns:
                    if cv == v and cs <= hour < ce and cm > mult:
                        mult = cm
                shares = Decimal(n)
                weighted = usd * (boosted * PD_MULT + (shares - boosted)) / supply[v]
                user_usd = usd * shares / supply[v]
                wk = self._bucket(hour - 1)
                slot = out.setdefault(u, {}).setdefault(wk, {"usd_hours": Decimal(0), "points": Decimal(0)})
                slot["usd_hours"] += user_usd
                slot["points"] += weighted * VAULT_RATE * mult
            hour += 3600
        return out

    @staticmethod
    def _bucket(ts: int) -> int:
        import core.rewards as rewards

        return rewards.bucket_for(ts, WEEK1)


@pytest.mark.parametrize("seed", [1, 7, 23])
def test_vault_accrual_matches_reference_model(_clean_rewards, seed):
    rewards = _clean_rewards
    uni = Universe(seed)
    uni.seed_db()

    now = (VAULT_START // 3600) * 3600
    rng = random.Random(seed * 31)
    while now < uni.horizon:
        now += rng.randint(1, 9) * 3600
        rewards.accrue_vaults(now_ts=min(now, uni.horizon))
    rewards.accrue_vaults(now_ts=uni.horizon)
    before = _all_contrib()
    rewards.accrue_vaults(now_ts=uni.horizon)
    assert _all_contrib() == before, "vault accrual must be idempotent"

    expected = uni.expected_vault_contrib()
    with storage.db_cursor() as cur:
        cur.execute("SELECT wallet, week_start, vault_usd_hours, points FROM crystal_rewards_contrib")
        got = {(str(w), int(ws)): (Decimal(v), Decimal(p)) for w, ws, v, p in cur.fetchall()}
    exp_flat = {
        (u, wk): (slot["usd_hours"], slot["points"])
        for u, weeks in expected.items()
        for wk, slot in weeks.items()
        if slot["points"] > 0
    }
    assert set(got.keys()) == set(exp_flat.keys()), f"contrib keys diverge (seed {seed})"
    for key, (ev, ep) in exp_flat.items():
        gv, gp = got[key]
        assert abs(gv - ev) <= Decimal("0.000001") * max(1, ev), f"usd_hours diverges at {key} (seed {seed})"
        assert abs(gp - ep) <= Decimal("0.000001") * max(1, ep), f"points diverge at {key} (seed {seed})"


def _all_contrib():
    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT wallet, week_start, vault_usd_hours, points FROM crystal_rewards_contrib ORDER BY wallet, week_start"
        )
        return [tuple(map(str, r)) for r in cur.fetchall()]


@pytest.mark.parametrize("seed", [3, 11])
def test_week_close_conserves_pool_and_ledger(_clean_rewards, seed):
    rewards = _clean_rewards
    uni = Universe(seed)
    uni.seed_db()
    rng = random.Random(seed * 17)

    with storage.db_cursor() as cur:
        for i, u in enumerate(uni.users):
            if rng.random() < 0.4 and i > 0:
                cur.execute(
                    "INSERT INTO referral_bindings (referee, referrer, block_number, log_index, timestamp)"
                    " VALUES (%s, %s, 1, %s, %s) ON CONFLICT DO NOTHING",
                    (u, uni.users[i - 1], i, WEEK1),
                )

    end = rewards.week_end_for(WEEK1)
    storage.set_meta("rewards_wm_launchpad", "999999999")
    storage.set_meta("rewards_wm_spot_taker", f"{end + 10}|zz|0")
    storage.set_meta("rewards_wm_spot_maker", f"{end + 10}|zz|0")
    while rewards.accrue_vaults(now_ts=end + 3600) > 0:
        pass
    with storage.db_cursor() as cur:
        cur.execute(
            "INSERT INTO crystal_vault_balance_samples (vault, block_number, timestamp, quote_balance, base_balance, usd_value)"
            " VALUES (%s, 999999, %s, 0, 0, 1)",
            (uni.vaults[0], end + 10),
        )
        cur.execute(
            "INSERT INTO launchpad_trades (block_number, log_index, timestamp, token, user_address, is_buy,"
            " native_amount, token_amount, usd_amount, price_native, txhash)"
            " VALUES (1, 0, %s, %s, %s, TRUE, 0, 0, 0, 0, '0xcap')",
            (end + 10, "0x" + "ee" * 20, uni.users[0]),
        )
        cur.execute(
            "INSERT INTO crystal_market_trades (txhash, log_index, block_number, timestamp, market, user_address,"
            " is_buy, amount_in, amount_out, start_price, end_price)"
            " VALUES ('0xcap', 0, 1, %s, %s, %s, TRUE, 0, 0, 0, 0)",
            (end + 10, "0x" + "e9" * 20, uni.users[0]),
        )

    closed = rewards.close_due_weeks(now_ts=end + 7200)
    if not closed:
        pytest.skip(f"seed {seed} produced no closable week")

    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT SUM(share), SUM(crystals), COUNT(*) FROM crystal_rewards_distributions WHERE week_start = %s",
            (WEEK1,),
        )
        share_sum, crystal_sum, n = cur.fetchone()
        if n:
            assert abs(Decimal(share_sum) - 1) < Decimal("0.0001"), "shares must sum to 1"
            assert Decimal(crystal_sum) <= Decimal(str(rewards.pool_size())) + Decimal("0.01"), "pool overspent"
        cur.execute(
            "SELECT rank, raw_points FROM crystal_rewards_distributions WHERE week_start = %s ORDER BY rank, wallet",
            (WEEK1,),
        )
        rows = cur.fetchall()
        for a, b in zip(rows, rows[1:]):
            assert Decimal(a[1]) >= Decimal(b[1]), "rank order must be monotone in points"

        cur.execute("SELECT wallet, crystals FROM crystal_rewards_balances ORDER BY wallet")
        balances = {str(w): Decimal(c) for w, c in cur.fetchall()}

    before = dict(balances)
    again = rewards._close_week(WEEK1, int(end + 9000))
    with storage.db_cursor() as cur:
        cur.execute("SELECT wallet, crystals FROM crystal_rewards_balances ORDER BY wallet")
        after = {str(w): Decimal(c) for w, c in cur.fetchall()}
    assert again is False and after == before, "closing a finalized week must change nothing"

    with storage.db_cursor() as cur:
        cur.execute("SELECT wallet, COALESCE(SUM(crystals), 0) FROM crystal_rewards_distributions GROUP BY wallet")
        dist = {str(w): Decimal(c) for w, c in cur.fetchall()}
        cur.execute("SELECT wallet, COALESCE(SUM(amount), 0) FROM crystal_rewards_grants GROUP BY wallet")
        grants = {str(w): Decimal(c) for w, c in cur.fetchall()}
        cur.execute(
            "SELECT referee, milestone, COUNT(*) FROM crystal_rewards_milestones GROUP BY referee, milestone HAVING COUNT(*) > 1"
        )
        assert cur.fetchall() == [], "milestones must be unique per referee"
    for w, bal in balances.items():
        assert bal == dist.get(w, Decimal(0)) + grants.get(w, Decimal(0)), f"ledger mismatch for {w}"
