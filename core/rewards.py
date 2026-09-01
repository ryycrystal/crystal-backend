from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import core.storage as storage
from core.storage.base import db_cursor

LA = ZoneInfo("America/Los_Angeles")

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
LVMON = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
NATIVE_EQUIV = {WMON, LVMON, NATIVE_SENTINEL}

DEFAULT_RATES = {
    "pregrad": 1.0,
    "grad": 0.10,
    "spot_taker": 0.05,
    "spot_maker": 0.01,
    "stable_taker": 0.01,
    "stable_maker": 0.002,
    "vault_hour": 0.05,
}
DEFAULT_POOL = 1_000_000.0
DEFAULT_EXPONENT = 0.8
DEFAULT_MILESTONES = [
    [1_000.0, 1_000.0, 1_000.0],
    [10_000.0, 2_000.0, 0.0],
    [100_000.0, 10_000.0, 0.0],
    [1_000_000.0, 50_000.0, 0.0],
]
STATUS_LADDER = [("diamond", 0.01), ("platinum", 0.10), ("gold", 0.25), ("silver", 0.50)]

BATCH = int(os.getenv("REWARDS_BATCH", "20000"))
POLL_SECONDS = int(os.getenv("REWARDS_POLL", "120"))
LEADER_TTL = int(os.getenv("REWARDS_LEADER_TTL", "300"))
MAX_VAULT_HOURS_PER_RUN = int(os.getenv("REWARDS_MAX_VAULT_HOURS", "72"))
VAULT_SAMPLE_STALENESS = 86400
MAX_WEEK_GAP_HOURS = int(os.getenv("REWARDS_MAX_GAP_HOURS", "6"))
NODE_ID = f"{os.getenv('HOSTNAME', 'node')}-{uuid.uuid4().hex[:8]}"


def _meta_float(key: str, default: float) -> float:
    try:
        raw = storage.get_meta(key)
        return float(raw) if raw is not None else default
    except Exception:
        return default


def _meta_json(key: str, default):
    try:
        raw = storage.get_meta(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


def rates() -> dict[str, float]:
    out = dict(DEFAULT_RATES)
    out.update({k: float(v) for k, v in _meta_json("rewards_rates", {}).items() if k in out})
    return out


def pool_size() -> float:
    return _meta_float("rewards_pool", DEFAULT_POOL)


def exponent() -> float:
    return _meta_float("rewards_exponent", DEFAULT_EXPONENT)


def gap_tolerance() -> int:
    return max(0, int(_meta_float("rewards_max_gap_hours", float(MAX_WEEK_GAP_HOURS))))


def gap_ack_key(week_start: int) -> str:
    return f"rewards_gaps_ack_{int(week_start)}"


def milestones() -> list[list[float]]:
    ms = _meta_json("rewards_milestones", DEFAULT_MILESTONES)
    return sorted([[float(a), float(b), float(c)] for a, b, c in ms], key=lambda m: m[0])


def _meta_ts(key: str, fallback: datetime) -> int:
    raw = storage.get_meta(key)
    if raw:
        try:
            return int(float(raw))
        except Exception:
            pass
    return int(fallback.timestamp())


def program_start_ts() -> int:
    return _meta_ts("rewards_program_start", datetime(2026, 9, 16, 0, 0, tzinfo=LA))


def vault_start_ts() -> int:
    return _meta_ts("rewards_vault_start", datetime(2026, 9, 8, 7, 0, tzinfo=LA))


def predeposit_cutoff_ts() -> int:
    raw = storage.get_meta("rewards_predeposit_cutoff")
    if raw:
        try:
            return int(float(raw))
        except Exception:
            pass
    return program_start_ts()


def predeposit_multiplier() -> float:
    return _meta_float("rewards_predeposit_multiplier", 3.0)


def predeposit_start_ts() -> int:
    raw = storage.get_meta("rewards_predeposit_start")
    if raw:
        try:
            return int(float(raw))
        except Exception:
            pass
    return vault_start_ts()


def week_start_for(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts), LA)
    days_since_wed = (dt.weekday() - 2) % 7
    d = (dt - timedelta(days=days_since_wed)).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=LA).timestamp())


def week_end_for(week_start: int) -> int:
    d = datetime.fromtimestamp(int(week_start), LA).date() + timedelta(days=7)
    return int(datetime(d.year, d.month, d.day, tzinfo=LA).timestamp())


def bucket_for(ts: int, main_start: int) -> int:
    ws = week_start_for(ts)
    return ws if ws >= main_start else main_start


def bucket_end(bucket: int) -> int:
    return week_end_for(week_start_for(bucket))


def _wm_get(key: str, default: str) -> str:
    raw = storage.get_meta(key)
    return raw if raw is not None else default


# accrual is a read-modify-write over a watermark and every contrib upsert is
# additive, so two workers running the same batch would double it. the read goes
# through the accruing transaction and every accrual transaction takes this lock
# first, which postgres releases on commit or rollback, so a second worker waits
# and then reads the advanced watermark instead of replaying the batch
REWARDS_LOCK_KEY = 782301944117


def _lock_accrual(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (REWARDS_LOCK_KEY,))


def _wm_read(cur, key: str, default: str) -> str:
    cur.execute("SELECT value FROM launchpad_kv WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else default


def _mon_usd_at(cur, ts: int) -> Decimal:
    cur.execute(
        """
        SELECT usd_amount, native_amount FROM launchpad_trades
        WHERE timestamp <= %s AND native_amount >= 10000000000000000 AND usd_amount > 0
        ORDER BY timestamp DESC LIMIT 1
        """,
        (int(ts),),
    )
    row = cur.fetchone()
    if row and row[1]:
        native = Decimal(int(row[1])) / Decimal(10**18)
        if native > 0:
            return Decimal(row[0]) / native
    try:
        px = storage.get_mon_price_usd()
        if px is not None and Decimal(px) > 0:
            return Decimal(px)
    except Exception:
        pass
    return Decimal("0.03")


class _QuotePricer:
    def __init__(self, cur, stables: set[str]):
        self.cur = cur
        self.stables = stables
        self._mon_by_hour: dict[int, Decimal] = {}

    def usd(self, quote_address: str, ts: int) -> Decimal:
        q = (quote_address or "").lower()
        if q in self.stables:
            return Decimal(1)
        if q in NATIVE_EQUIV:
            hour = int(ts) // 3600
            if hour not in self._mon_by_hour:
                self._mon_by_hour[hour] = _mon_usd_at(self.cur, ts)
            return self._mon_by_hour[hour]
        return Decimal(0)


def _markets_meta(cur) -> dict[str, dict]:
    cur.execute(
        """
        SELECT market, quote_address, quote_decimals, base_address
        FROM crystal_markets
        """
    )
    out = {}
    for m, qa, qd, ba in cur.fetchall():
        out[str(m).lower()] = {
            "quote_address": str(qa).lower(),
            "quote_decimals": int(qd or 18),
            "base_address": str(ba).lower(),
        }
    return out


def _is_stable_market(meta: dict, stables: set[str]) -> bool:
    return meta["quote_address"] in stables and meta["base_address"] in stables


def accrue_launchpad(guard=None) -> int:
    start_ts = program_start_ts()
    r = rates()
    processed = 0
    while True:
        if guard is not None and not guard():
            return processed
        with db_cursor() as cur:
            _lock_accrual(cur)
            wm = int(_wm_read(cur, "rewards_wm_launchpad", "0"))
            deny = storage.rewards_denylist(cur)
            cur.execute(
                """
                SELECT t.id, t.timestamp, t.user_address, t.usd_amount,
                       tok.migrated, tok.migrated_at
                FROM launchpad_trades t
                LEFT JOIN launchpad_tokens tok ON tok.token = t.token
                WHERE t.id > %s
                ORDER BY t.id
                LIMIT %s
                """,
                (wm, BATCH),
            )
            rows = cur.fetchall()
            if not rows:
                return processed
            now_ts = int(time.time())
            for rid, ts, user, usd, migrated, migrated_at in rows:
                ts = int(ts)
                if ts < start_ts:
                    continue
                user = str(user or "").lower()
                if not user or user in deny:
                    continue
                usd = Decimal(usd or 0)
                if usd <= 0:
                    continue
                graduated = bool(migrated) and migrated_at is not None and ts >= int(migrated_at)
                rate = r["grad"] if graduated else r["pregrad"]
                field = "grad_usd" if graduated else "pregrad_usd"
                storage.add_rewards_contrib(
                    cur,
                    bucket_for(ts, start_ts),
                    user,
                    now_ts,
                    **{field: usd, "points": usd * Decimal(str(rate))},
                )
            storage.set_meta("rewards_wm_launchpad", str(int(rows[-1][0])), cur=cur)
            processed += len(rows)
        if len(rows) < BATCH:
            return processed


def _parse_keyset(raw: str) -> tuple[int, str, int]:
    try:
        ts, tx, li = raw.split("|")
        return int(ts), tx, int(li)
    except Exception:
        return 0, "", -1


def accrue_spot_takers(guard=None) -> int:
    start_ts = program_start_ts()
    r = rates()
    processed = 0
    while True:
        if guard is not None and not guard():
            return processed
        with db_cursor() as cur:
            _lock_accrual(cur)
            wm_ts, wm_tx, wm_li = _parse_keyset(_wm_read(cur, "rewards_wm_spot_taker", "0||-1"))
            deny = storage.rewards_denylist(cur)
            stables = storage.rewards_stable_tokens(cur)
            markets = _markets_meta(cur)
            pricer = _QuotePricer(cur, stables)
            cur.execute(
                """
                SELECT timestamp, txhash, log_index, market, user_address, is_buy, amount_in, amount_out
                FROM crystal_market_trades
                WHERE (timestamp, txhash, log_index) > (%s, %s, %s)
                ORDER BY timestamp, txhash, log_index
                LIMIT %s
                """,
                (wm_ts, wm_tx, wm_li, BATCH),
            )
            rows = cur.fetchall()
            if not rows:
                return processed
            txhashes = sorted({str(t[1]) for t in rows})
            cur.execute(
                """
                SELECT txhash, market, maker,
                       SUM(CASE WHEN maker_is_buy THEN amount_out ELSE amount_high END)
                FROM crystal_orderbook_fills
                WHERE txhash = ANY(%s)
                GROUP BY txhash, market, maker
                """,
                (txhashes,),
            )
            self_fills = {
                (str(a), str(b).lower(), str(c).lower()): Decimal(int(d or 0)) for a, b, c, d in cur.fetchall()
            }
            now_ts = int(time.time())
            for ts, tx, li, market, user, is_buy, amount_in, amount_out in rows:
                ts = int(ts)
                if ts < start_ts:
                    continue
                user = str(user or "").lower()
                meta = markets.get(str(market).lower())
                if not user or user in deny or meta is None:
                    continue
                quote_amt = Decimal(int((amount_in if is_buy else amount_out) or 0))
                quote_amt -= self_fills.get((str(tx), str(market).lower(), user), Decimal(0))
                if quote_amt <= 0:
                    continue
                px = pricer.usd(meta["quote_address"], ts)
                if px <= 0:
                    continue
                usd = quote_amt / Decimal(10 ** meta["quote_decimals"]) * px
                stable = _is_stable_market(meta, stables)
                rate = r["stable_taker"] if stable else r["spot_taker"]
                field = "stable_taker_usd" if stable else "spot_taker_usd"
                storage.add_rewards_contrib(
                    cur,
                    bucket_for(ts, start_ts),
                    user,
                    now_ts,
                    **{field: usd, "points": usd * Decimal(str(rate))},
                )
            last = rows[-1]
            storage.set_meta("rewards_wm_spot_taker", f"{int(last[0])}|{last[1]}|{int(last[2])}", cur=cur)
            processed += len(rows)
        if len(rows) < BATCH:
            return processed


def accrue_spot_makers(guard=None) -> int:
    start_ts = program_start_ts()
    r = rates()
    processed = 0
    while True:
        if guard is not None and not guard():
            return processed
        with db_cursor() as cur:
            _lock_accrual(cur)
            wm_ts, wm_tx, wm_li = _parse_keyset(_wm_read(cur, "rewards_wm_spot_maker", "0||-1"))
            deny = storage.rewards_denylist(cur)
            stables = storage.rewards_stable_tokens(cur)
            markets = _markets_meta(cur)
            pricer = _QuotePricer(cur, stables)
            cur.execute(
                """
                SELECT timestamp, txhash, log_index, market, maker, maker_is_buy, amount_high, amount_out
                FROM crystal_orderbook_fills
                WHERE (timestamp, txhash, log_index) > (%s, %s, %s)
                ORDER BY timestamp, txhash, log_index
                LIMIT %s
                """,
                (wm_ts, wm_tx, wm_li, BATCH),
            )
            rows = cur.fetchall()
            if not rows:
                return processed
            txhashes = sorted({str(t[1]) for t in rows})
            cur.execute(
                """
                SELECT txhash, market, user_address FROM crystal_market_trades
                WHERE txhash = ANY(%s)
                """,
                (txhashes,),
            )
            takers: dict[tuple[str, str], set[str]] = {}
            for tx, mkt, taker in cur.fetchall():
                takers.setdefault((str(tx), str(mkt).lower()), set()).add(str(taker).lower())
            now_ts = int(time.time())
            for ts, tx, li, market, maker, maker_is_buy, amount_high, amount_out in rows:
                ts = int(ts)
                if ts < start_ts:
                    continue
                maker = str(maker or "").lower()
                meta = markets.get(str(market).lower())
                if not maker or maker in deny or meta is None:
                    continue
                if maker in takers.get((str(tx), str(market).lower()), set()):
                    continue
                quote_amt = Decimal(int((amount_out if maker_is_buy else amount_high) or 0))
                if quote_amt <= 0:
                    continue
                px = pricer.usd(meta["quote_address"], ts)
                if px <= 0:
                    continue
                usd = quote_amt / Decimal(10 ** meta["quote_decimals"]) * px
                stable = _is_stable_market(meta, stables)
                rate = r["stable_maker"] if stable else r["spot_maker"]
                field = "stable_maker_usd" if stable else "spot_maker_usd"
                storage.add_rewards_contrib(
                    cur,
                    bucket_for(ts, start_ts),
                    maker,
                    now_ts,
                    **{field: usd, "points": usd * Decimal(str(rate))},
                )
            last = rows[-1]
            storage.set_meta("rewards_wm_spot_maker", f"{int(last[0])}|{last[1]}|{int(last[2])}", cur=cur)
            processed += len(rows)
        if len(rows) < BATCH:
            return processed


def _campaign_multiplier(campaigns, vault: str, ts: int) -> Decimal:
    best = Decimal(1)
    for v, mult, s, e in campaigns:
        if v == vault and s <= ts < e and Decimal(str(mult)) > best:
            best = Decimal(str(mult))
    return best


def accrue_vaults(now_ts: int | None = None, guard=None) -> int:
    main_start = program_start_ts()
    start_ts = min(vault_start_ts(), main_start)
    cutoff = predeposit_cutoff_ts()
    pd_start = predeposit_start_ts()
    pd_mult = Decimal(str(predeposit_multiplier()))
    now_ts = int(now_ts if now_ts is not None else time.time())
    r = rates()
    rate = Decimal(str(r["vault_hour"]))
    hours_done = 0
    while hours_done < MAX_VAULT_HOURS_PER_RUN:
        if guard is not None and not guard():
            return hours_done
        with db_cursor() as cur:
            _lock_accrual(cur)
            wm = int(_wm_read(cur, "rewards_wm_vault_hour", "0"))
            if wm <= 0:
                wm = (max(start_ts, 0) // 3600) * 3600
            hour = wm + 3600
            if hour > now_ts:
                return hours_done
            if hour < start_ts:
                storage.set_meta("rewards_wm_vault_hour", str((start_ts // 3600) * 3600), cur=cur)
                continue
            deny = storage.rewards_denylist(cur)
            campaigns = storage.rewards_campaigns(cur)
            pd_vaults = storage.rewards_predeposit_vaults(cur)
            cur.execute(
                """
                SELECT vault, user_address, delta, ts FROM (
                    SELECT vault, user_address, shares AS delta, timestamp AS ts,
                           block_number, log_index
                    FROM crystal_vault_deposits WHERE timestamp <= %s
                    UNION ALL
                    SELECT vault, user_address, -shares, timestamp,
                           block_number, log_index
                    FROM crystal_vault_withdrawals WHERE timestamp <= %s
                ) e
                ORDER BY vault, user_address, ts, block_number, log_index
                """,
                (hour, hour),
            )
            # one ordered pass splits every position into shares that earned the
            # pre-deposit boost and shares that did not. only deposits inside the
            # announced window are boosted, and a withdrawal spends unboosted
            # shares first so the boost survives ordinary trimming but is burned
            # for good once someone dips below what they pre-deposited
            books: dict[tuple[str, str], list[Decimal]] = {}
            for v, u, delta, ts in cur.fetchall():
                key = (str(v).lower(), str(u).lower())
                book = books.setdefault(key, [Decimal(0), Decimal(0)])
                amount = Decimal(int(delta))
                if amount >= 0:
                    boosted_deposit = pd_start <= int(ts) <= cutoff
                    book[0 if boosted_deposit else 1] += amount
                else:
                    owed = -amount
                    spend = min(owed, book[1])
                    book[1] -= spend
                    owed -= spend
                    if owed > 0:
                        book[0] = max(Decimal(0), book[0] - owed)
            holdings = [(v, u, b[0] + b[1], b[0]) for (v, u), b in books.items() if b[0] + b[1] > 0]
            allowance = {(v, u): boost for v, u, _total, boost in holdings}

            if holdings:
                vaults = sorted({str(h[0]).lower() for h in holdings})
                cur.execute(
                    """
                    SELECT DISTINCT ON (vault) vault, usd_value, timestamp
                    FROM crystal_vault_balance_samples
                    WHERE vault = ANY(%s) AND timestamp <= %s
                    ORDER BY vault, timestamp DESC
                    """,
                    (vaults, hour),
                )
                samples = {str(v).lower(): (Decimal(u or 0), int(sts)) for v, u, sts in cur.fetchall()}
                vault_usd = {
                    v: usd for v, (usd, sts) in samples.items() if sts >= hour - VAULT_SAMPLE_STALENESS
                }
                supply: dict[str, Decimal] = {}
                holder_count: dict[str, int] = {}
                for v, _u, sh, _pn in holdings:
                    vl = str(v).lower()
                    supply[vl] = supply.get(vl, Decimal(0)) + Decimal(int(sh))
                    holder_count[vl] = holder_count.get(vl, 0) + 1
                ws = bucket_for(hour - 1, main_start)
                # a vault holding shares that cannot be valued this hour earns nothing
                # for everyone in it, and nothing downstream would ever say so. record
                # the hour so the close can refuse to bake a sampling outage into
                # permanent balances
                valued: list[str] = []
                for vl, sup in supply.items():
                    if vault_usd.get(vl, Decimal(0)) > 0 and sup > 0:
                        valued.append(vl)
                        continue
                    last_usd, last_ts = samples.get(vl, (Decimal(0), 0))
                    if last_ts <= 0:
                        reason = "no_sample"
                    elif last_ts < hour - VAULT_SAMPLE_STALENESS:
                        reason = "stale_sample"
                    elif last_usd <= 0:
                        reason = "zero_value"
                    else:
                        reason = "no_supply"
                    if storage.record_vault_gap(cur, hour, vl, ws, holder_count.get(vl, 0), sup, last_ts, reason):
                        print(
                            f"[REWARDS] vault {vl} unvalued at hour {hour} ({reason}, "
                            f"last sample {last_ts}); {holder_count.get(vl, 0)} holders earned nothing",
                            flush=True,
                        )
                if valued:
                    cur.execute(
                        "DELETE FROM crystal_rewards_vault_gaps WHERE hour = %s AND vault = ANY(%s)",
                        (hour, valued),
                    )
                for v, user, sh, _pn in holdings:
                    v = str(v).lower()
                    user = str(user).lower()
                    if user in deny:
                        continue
                    usd_total = vault_usd.get(v, Decimal(0))
                    sup = supply.get(v, Decimal(0))
                    if usd_total <= 0 or sup <= 0:
                        continue
                    shares = Decimal(int(sh))
                    boosted = min(shares, allowance.get((v, user), Decimal(0)))
                    if pd_vaults and v not in pd_vaults:
                        boosted = Decimal(0)
                    user_usd = usd_total * shares / sup
                    weighted = usd_total * (boosted * pd_mult + (shares - boosted)) / sup
                    mult = _campaign_multiplier(campaigns, v, hour)
                    storage.add_rewards_contrib(
                        cur,
                        ws,
                        user,
                        now_ts,
                        vault_usd_hours=user_usd,
                        points=weighted * rate * mult,
                    )
            storage.set_meta("rewards_wm_vault_hour", str(hour), cur=cur)
            hours_done += 1
    return hours_done


def _status_for(rank: int, total: int) -> str:
    for name, q in STATUS_LADDER:
        if rank <= max(1, math.ceil(total * q)):
            return name
    return "bronze"


def _sources_caught_up(cur, week_end: int) -> bool:
    cur.execute(
        """
        SELECT GREATEST(
            COALESCE((SELECT MAX(timestamp) FROM launchpad_trades), 0),
            COALESCE((SELECT MAX(timestamp) FROM crystal_market_trades), 0),
            COALESCE((SELECT MAX(timestamp) FROM crystal_vault_balance_samples), 0)
        )
        """
    )
    row = cur.fetchone()
    if not row or row[0] is None or int(row[0]) < week_end:
        return False
    wm = int(_wm_get("rewards_wm_launchpad", "0"))
    cur.execute(
        "SELECT 1 FROM launchpad_trades WHERE id > %s AND timestamp < %s LIMIT 1",
        (wm, week_end),
    )
    if cur.fetchone():
        return False
    for key, table in (
        ("rewards_wm_spot_taker", "crystal_market_trades"),
        ("rewards_wm_spot_maker", "crystal_orderbook_fills"),
    ):
        ts, tx, li = _parse_keyset(_wm_get(key, "0||-1"))
        cur.execute(
            f"""
            SELECT 1 FROM {table}
            WHERE (timestamp, txhash, log_index) > (%s, %s, %s) AND timestamp < %s
            LIMIT 1
            """,
            (ts, tx, li, week_end),
        )
        if cur.fetchone():
            return False
    if int(_wm_get("rewards_wm_vault_hour", "0")) < week_end:
        return False
    return True


def _grant(cur, wallet: str, amount: Decimal, kind: str, ref: str, now_ts: int, balances: dict[str, Decimal]) -> None:
    storage.add_rewards_balance(cur, wallet, amount, now_ts)
    cur.execute(
        "INSERT INTO crystal_rewards_grants (ts, wallet, kind, amount, ref) VALUES (%s, %s, %s, %s, %s)",
        (now_ts, wallet, kind, amount, ref),
    )
    balances[wallet] = balances.get(wallet, Decimal(0)) + amount


def _settle_milestones(cur, prev: dict[str, Decimal], balances: dict[str, Decimal], now_ts: int) -> None:
    ms = milestones()
    deny = storage.rewards_denylist(cur)
    for _round in range(6):
        changed = False
        for wallet in list(balances.keys()):
            before = prev.get(wallet, Decimal(0))
            after = balances[wallet]
            if after <= before:
                continue
            for threshold, referrer_reward, welcome in ms:
                th = Decimal(str(threshold))
                if not (before < th <= after):
                    continue
                binding = storage.get_referral_binding(wallet)
                referrer = str(binding[0]).lower() if binding and binding[0] else ""
                if not referrer or referrer == wallet or referrer in deny:
                    continue
                cur.execute(
                    """
                    INSERT INTO crystal_rewards_milestones (referee, milestone, granted_at, referrer)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (referee, milestone) DO NOTHING
                    RETURNING referee
                    """,
                    (wallet, th, now_ts, referrer),
                )
                if not cur.fetchone():
                    continue
                if referrer not in prev:
                    prev[referrer] = Decimal(str(storage.get_rewards_balance(cur, referrer)))
                    balances.setdefault(referrer, prev[referrer])
                if Decimal(str(referrer_reward)) > 0:
                    _grant(cur, referrer, Decimal(str(referrer_reward)), "referral", wallet, now_ts, balances)
                    changed = True
                if Decimal(str(welcome)) > 0:
                    _grant(cur, wallet, Decimal(str(welcome)), "welcome", referrer, now_ts, balances)
                    changed = True
            prev[wallet] = after
        if not changed:
            return


def close_due_weeks(now_ts: int | None = None) -> list[int]:
    now_ts = int(now_ts if now_ts is not None else time.time())
    start = program_start_ts()
    if now_ts <= start:
        return []
    closed: list[int] = []
    ws = start
    while bucket_end(ws) <= now_ts:
        closed_one = _close_week(ws, now_ts)
        if closed_one is None:
            break
        if closed_one:
            closed.append(ws)
        ws = bucket_end(ws)
    return closed


def _close_week(week_start: int, now_ts: int) -> bool | None:
    week_end = bucket_end(week_start)
    with db_cursor() as cur:
        cur.execute("SELECT finalized FROM crystal_rewards_weeks WHERE week_start = %s", (week_start,))
        row = cur.fetchone()
        if row and bool(row[0]):
            return False
        if not _sources_caught_up(cur, week_end):
            return None
        # distributions are permanent, so never finalize a week whose vault hours
        # were valued at zero by a sampling outage. the week simply stays open and
        # retries until the samples backfill or an admin acknowledges the gap
        gap_vault, gap_hours = storage.worst_vault_gap(cur, week_start)
        tolerance = gap_tolerance()
        if gap_hours > tolerance and _wm_read(cur, gap_ack_key(week_start), "") != "1":
            print(
                f"[REWARDS] refusing to close week {week_start}: vault {gap_vault} had "
                f"{gap_hours} unvalued hours (tolerance {tolerance}); backfill the samples "
                f"or acknowledge with POST {os.getenv('REWARDS_PATH_PREFIX', 'results')}/acknowledge-gaps",
                flush=True,
            )
            return None
        deny = storage.rewards_denylist(cur)
        cur.execute(
            """
            SELECT wallet, points FROM crystal_rewards_contrib
            WHERE week_start = %s AND points > 0
            ORDER BY points DESC, wallet
            """,
            (week_start,),
        )
        rows = [(str(w).lower(), Decimal(p)) for w, p in cur.fetchall() if str(w).lower() not in deny]
        pool = Decimal(str(pool_size()))
        exp = exponent()
        total = len(rows)
        adjusted = [Decimal(str(math.pow(float(p), exp))) for _w, p in rows]
        total_adj = sum(adjusted, Decimal(0))
        total_raw = sum((p for _w, p in rows), Decimal(0))
        prev_balances: dict[str, Decimal] = {}
        balances: dict[str, Decimal] = {}
        rank = 0
        for i, (wallet, points) in enumerate(rows):
            if i == 0 or points < rows[i - 1][1]:
                rank = i + 1
            share = (adjusted[i] / total_adj) if total_adj > 0 else Decimal(0)
            crystals = (pool * share).quantize(Decimal("0.000001"))
            status = _status_for(rank, total)
            cur.execute(
                """
                INSERT INTO crystal_rewards_distributions
                    (week_start, wallet, raw_points, adjusted, share, crystals, rank, participants, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (week_start, wallet) DO NOTHING
                """,
                (week_start, wallet, points, adjusted[i], share, crystals, rank, total, status),
            )
            if cur.rowcount != 1:
                continue
            prev_balances[wallet] = Decimal(str(storage.get_rewards_balance(cur, wallet)))
            storage.add_rewards_balance(cur, wallet, crystals, now_ts)
            balances[wallet] = prev_balances[wallet] + crystals
        cur.execute(
            """
            INSERT INTO crystal_rewards_weeks
                (week_start, week_end, pool, exponent, participants, total_raw, total_adjusted, finalized, finalized_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
            ON CONFLICT (week_start) DO UPDATE
            SET finalized = TRUE, finalized_at = EXCLUDED.finalized_at,
                participants = EXCLUDED.participants, total_raw = EXCLUDED.total_raw,
                total_adjusted = EXCLUDED.total_adjusted
            """,
            (week_start, week_end, pool, Decimal(str(exp)), total, total_raw, total_adj, now_ts),
        )
        _settle_milestones(cur, prev_balances, balances, now_ts)
    return True


def run_once(now_ts: int | None = None, leader_holder: str | None = None) -> dict:
    storage.ensure_rewards_tables()
    guard = (lambda: storage.claim_rewards_leader(leader_holder, LEADER_TTL)) if leader_holder else (lambda: True)
    out = {
        "launchpad": accrue_launchpad(guard),
        "spot_takers": accrue_spot_takers(guard),
        "spot_makers": accrue_spot_makers(guard),
        "vault_hours": accrue_vaults(now_ts, guard),
        "weeks_closed": close_due_weeks(now_ts) if guard() else [],
    }
    return out


def _loop() -> None:
    while True:
        try:
            if storage.claim_rewards_leader(NODE_ID, LEADER_TTL):
                run_once(leader_holder=NODE_ID)
        except Exception as e:
            print(f"[REWARDS] cycle failed: {e!r}", flush=True)
        time.sleep(POLL_SECONDS)


def start_worker() -> None:
    if os.getenv("REWARDS_WORKER", "1") != "1":
        return
    threading.Thread(target=_loop, daemon=True).start()
