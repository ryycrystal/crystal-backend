from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from api.api import _sample_evenly_by_time, storage, time, ttl_cache
from state import RPC_HTTP, State

router = APIRouter()


def _parse_timeframe(value: Any) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="invalid timeframe")
    if isinstance(value, int):
        if value in {1, 2, 3, 4}:
            return int(value)
        raise HTTPException(status_code=400, detail="invalid timeframe")
    sval = str(value or "").strip().lower().replace("_", "-")
    tf_map = {
        "1": 1,
        "1d": 1,
        "day": 1,
        "24h": 1,
        "24hr": 1,
        "24hrs": 1,
        "2": 2,
        "1w": 2,
        "week": 2,
        "7d": 2,
        "3": 3,
        "1m": 3,
        "month": 3,
        "30d": 3,
        "4": 4,
        "all": 4,
        "all-time": 4,
        "alltime": 4,
        "lifetime": 4,
    }
    tf = tf_map.get(sval)
    if tf is None:
        raise HTTPException(status_code=400, detail="invalid timeframe")
    return int(tf)


def _rpc_jsonrpc_sync(method: str, params: list[Any]) -> dict:
    resp = httpx.post(
        RPC_HTTP,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("invalid rpc response")
    if data.get("error") is not None:
        raise ValueError(str(data.get("error")))
    return data


import threading as _threading
from decimal import Decimal

_STATE_CACHE: dict[str, Any] = {"state": None, "ts": 0.0}
_STATE_CACHE_LOCK = _threading.Lock()
_STATE_CACHE_TTL = 30.0


def _cached_state():
    now = time.time()
    st = _STATE_CACHE["state"]
    if st is not None and (now - float(_STATE_CACHE["ts"])) < _STATE_CACHE_TTL:
        return st
    with _STATE_CACHE_LOCK:
        st = _STATE_CACHE["state"]
        if st is not None and (time.time() - float(_STATE_CACHE["ts"])) < _STATE_CACHE_TTL:
            return st
        new_st = State()
        new_st.rebuild_from_db()
        _STATE_CACHE["state"] = new_st
        _STATE_CACHE["ts"] = time.time()
        return new_st


def _vault_usd_from_state(st, quote, base, quote_bal, base_bal, qd, bd) -> float:
    try:
        pq = Decimal(str(st.tokenToPrice.get((quote or "").lower(), 0) or 0))
        pb = Decimal(str(st.tokenToPrice.get((base or "").lower(), 0) or 0))
        qd = int(qd or 0)
        bd = int(bd or 0)
        q_units = Decimal(int(quote_bal)) / (Decimal(10) ** qd if qd >= 0 else Decimal(1))
        b_units = Decimal(int(base_bal)) / (Decimal(10) ** bd if bd >= 0 else Decimal(1))
        val = (q_units * pq) + (b_units * pb)
        return float(val) if val.is_finite() else 0.0
    except Exception:
        return 0.0


def _vault_user_lockup_fields(
    *,
    now_ts: int,
    lockup_seconds: int,
    user_row,
    user_shares: int,
) -> dict[str, int | bool]:
    last_deposit = int(user_row[3] or 0) if user_row and len(user_row) > 3 else 0
    last_withdraw = int(user_row[4] or 0) if user_row and len(user_row) > 4 else 0
    lockup_i = int(lockup_seconds or 0)
    unlock_at = (last_deposit + lockup_i) if (last_deposit > 0 and lockup_i > 0) else 0
    locked = bool(int(user_shares or 0) > 0 and unlock_at > int(now_ts))
    remaining = max(0, int(unlock_at) - int(now_ts)) if locked else 0
    return {
        "lastDeposit": last_deposit,
        "lastWithdraw": last_withdraw,
        "withdrawLocked": locked,
        "withdrawUnlockAt": int(unlock_at or 0),
        "withdrawLockupRemaining": int(remaining),
    }


def _per_share_pnl_series(pts: list[dict]) -> list[dict]:
    out = []
    base_vps = None
    prev_vps = None
    cum_usd = 0.0
    last_pct = 0.0
    for p in pts:
        sh = int(p.get("shares") or 0)
        usd = float(p.get("usdValue") or 0.0)
        if sh > 0 and usd > 0:
            vps = usd / sh
            if base_vps is None:
                base_vps = vps
                prev_vps = vps
            cum_usd += float(sh) * (vps - prev_vps)
            prev_vps = vps
            last_pct = ((vps / base_vps) - 1.0) * 100.0
        out.append({"timestamp": int(p["timestamp"]), "pnlUsd": cum_usd, "pnlPct": last_pct})
    return out


VAULT_APY_MIN_WINDOW_SECS = 6 * 3600


def _vault_apy(vault_addr: str, window_days: int = 7) -> tuple[float, int] | None:
    now_ts = int(time.time())
    try:
        rows = storage.list_crystal_vault_balance_samples(vault_addr, start_ts=now_ts - window_days * 86400, limit=0)
    except Exception:
        return None
    shared = [
        (int(r[1] or 0), float(r[4] or 0.0), int(r[5] or 0))
        for r in rows
        if len(r) > 5 and int(r[5] or 0) > 0 and float(r[4] or 0.0) > 0
    ]
    if len(shared) < 2:
        return None
    shared.sort(key=lambda p: p[0])
    t_first, usd_first, sh_first = shared[0]
    t_last, usd_last, sh_last = shared[-1]
    window = t_last - t_first
    if window < VAULT_APY_MIN_WINDOW_SECS:
        return None
    first_vps = usd_first / sh_first
    last_vps = usd_last / sh_last
    if first_vps <= 0:
        return None
    ratio = last_vps / first_vps
    if ratio <= 0:
        return None
    periods = 365.0 * 86400.0 / window
    try:
        apy = (ratio**periods - 1.0) * 100.0
    except OverflowError:
        apy = float("inf")
    if not (apy == apy):
        return None
    apy = min(apy, 100000.0)
    return (apy, window)


def _vault_apy_pct(vault_addr: str, window_days: int = 7) -> float | None:
    out = _vault_apy(vault_addr, window_days)
    return out[0] if out else None


def _avg_cost_from_flows(flows: list[tuple[int, float]]) -> tuple[int, float, float]:
    pos = 0
    cost = 0.0
    realized = 0.0
    for shares, nav in flows:
        if shares > 0:
            cost += shares * nav
            pos += int(shares)
        elif shares < 0 and pos > 0:
            take = min(int(-shares), pos)
            avg = cost / pos
            realized += take * (nav - avg)
            cost -= take * avg
            pos -= take
    entry = (cost / pos) if pos > 0 else 0.0
    return pos, entry, realized


def _nav_at_flow(vault_addr: str, ts: int, circ_now: int) -> tuple[float, int, bool] | None:
    minted_incl, burned_incl = storage.sum_vault_share_flows_after(vault_addr, ts, inclusive=True)
    supply_before = circ_now - minted_incl + burned_incl
    before = storage.vault_sample_usd_before(vault_addr, ts)
    if before is not None and supply_before > 0:
        return (before[1] / supply_before, before[0], False)
    minted_after, burned_after = storage.sum_vault_share_flows_after(vault_addr, ts)
    supply_after = circ_now - minted_after + burned_after
    after = storage.vault_sample_usd_at_or_after(vault_addr, ts)
    if after is not None and supply_after > 0:
        return (after[1] / supply_after, after[0], True)
    return None


def _vault_user_pnl(
    vault_addr: str, user_addr: str, circ_now: int, tvl_usd_now: float, nav_now: float | None = None
) -> dict | None:
    flows = storage.list_crystal_vault_user_flows(vault_addr, user_addr)
    if not flows or circ_now <= 0:
        return None
    priced: list[tuple[int, float]] = []
    entry_estimated = False
    tracked_since = None
    for ts, shares in flows:
        nav = _nav_at_flow(vault_addr, ts, circ_now)
        if nav is None or nav[0] <= 0:
            return None
        nav_val, sample_ts, estimated = nav
        entry_estimated = entry_estimated or estimated
        if tracked_since is None or sample_ts < tracked_since:
            tracked_since = sample_ts
        priced.append((shares, nav_val))
    pos, entry_nav, realized = _avg_cost_from_flows(priced)
    if nav_now is None:
        nav_now = (tvl_usd_now / circ_now) if tvl_usd_now > 0 else None
    if nav_now is None or nav_now <= 0:
        return None
    if pos <= 0 or entry_nav <= 0:
        return {
            "entryNav": None,
            "navNow": nav_now,
            "unrealizedPnlUsd": 0.0,
            "unrealizedPnlPct": 0.0,
            "realizedPnlUsd": realized,
            "entryEstimated": entry_estimated,
            "trackedSince": tracked_since,
        }
    return {
        "entryNav": entry_nav,
        "navNow": nav_now,
        "unrealizedPnlUsd": pos * (nav_now - entry_nav),
        "unrealizedPnlPct": ((nav_now / entry_nav) - 1.0) * 100.0,
        "realizedPnlUsd": realized,
        "entryEstimated": entry_estimated,
        "trackedSince": tracked_since,
    }


def _per_share_pct_change(pts: list[dict]) -> float:
    shared = [p for p in pts if int(p.get("shares") or 0) > 0 and float(p.get("usdValue") or 0.0) > 0]
    if not shared:
        return 0.0
    first_vps = float(shared[0]["usdValue"]) / float(shared[0]["shares"])
    last_vps = float(shared[-1]["usdValue"]) / float(shared[-1]["shares"])
    return ((last_vps / first_vps) - 1.0) * 100.0 if first_vps > 0 else 0.0


def _vault_snapshot_from_samples(vault_addr: str, timeframe: int = 1, points: int = 0) -> dict | None:
    now_ts = int(time.time())
    if timeframe == 1:
        start_ts = now_ts - 86400
    elif timeframe == 2:
        start_ts = now_ts - (7 * 86400)
    elif timeframe == 3:
        start_ts = now_ts - (30 * 86400)
    else:
        start_ts = None

    rows = storage.list_crystal_vault_balance_samples(vault_addr, start_ts=start_ts, limit=0)
    if not rows:
        return None

    pts = [
        {
            "block": int(r[0] or 0),
            "timestamp": int(r[1] or 0),
            "usdValue": float(r[4] or 0.0),
            "shares": int(r[5] or 0) if len(r) > 5 else 0,
        }
        for r in rows
    ]
    pts.sort(key=lambda p: int(p["timestamp"]))

    values = [float(p["usdValue"]) for p in pts]
    last = values[-1] if values else 0.0
    pct = _per_share_pct_change(pts)

    if points and int(points) > 0:
        pts = _sample_evenly_by_time(pts, int(points), lambda p: int(p.get("timestamp") or 0))

    tvl = [[int(p["timestamp"]), float(p["usdValue"])] for p in pts]
    return {
        "timeframe": int(timeframe),
        "tvl": tvl,
        "stats": {
            "pctChange": pct,
            "lastUsd": last,
            "min": min(values) if values else 0.0,
            "max": max(values) if values else 0.0,
        },
    }


@router.get("/vaults/list")
@ttl_cache("vaults:list", ttl_seconds=3)
def list_vaults(
    user: str | None = Query(None, description="optional user address for userShares enrichment"),
    search: str | None = Query(None, min_length=0, max_length=128),
    status: str = Query("all", description="all | active | closed"),
    sort: str = Query("latest_deposit", description="latest_deposit | tvl | user_position"),
    order: str = Query("desc", description="asc | desc"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=50),
    include_snapshot: bool = Query(True),
    snapshot_timeframe: str = Query("1", description="1|2|3|4 or 1d|1w|1m|all-time"),
    snapshot_points: int = Query(48, ge=0, le=200),
) -> dict[str, Any]:
    status_norm = (status if isinstance(status, str) else "all" or "all").strip().lower()
    if status_norm not in {"all", "active", "closed"}:
        raise HTTPException(status_code=400, detail="invalid status")
    sort_norm = (sort if isinstance(sort, str) else "latest_deposit" or "latest_deposit").strip().lower()
    if sort_norm not in {"latest_deposit", "tvl", "user_position"}:
        raise HTTPException(status_code=400, detail="invalid sort")
    order_norm = (order if isinstance(order, str) else "desc" or "desc").strip().lower()
    if order_norm not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="invalid order")
    user_addr = (user or "0x0000000000000000000000000000000000000000").lower()
    timeframe_i = _parse_timeframe(snapshot_timeframe)
    rows = storage.list_crystal_vaults_page(
        user_address=user_addr,
        search=(search or ""),
        status=status_norm,
        page=int(page),
        limit=int(limit),
        sort_by=sort_norm,
        sort_dir=order_norm,
    )

    items = []
    total_count = int(rows[0][-1] or 0) if rows else 0
    for r in rows:
        (
            vault_addr,
            owner,
            quote,
            base,
            market,
            name,
            description,
            social1,
            social2,
            social3,
            locked,
            closed,
            max_shares,
            circulating_shares,
            quote_decimals,
            base_decimals,
            lockup,
            decrease_on_withdraw,
            latest_block,
            latest_ts,
            latest_quote_balance,
            latest_base_balance,
            latest_usd_value,
            user_shares,
            last_deposit,
            quote_ticker,
            base_ticker,
            quote_name,
            base_name,
            _row_total_count,
        ) = r
        circ = int(circulating_shares or 0)
        u_shares = int(user_shares or 0)
        user_share_pct = (u_shares / circ) if circ > 0 and u_shares > 0 else 0.0
        row_apy = _vault_apy(str(vault_addr).lower())
        snapshot = None
        if include_snapshot:
            snapshot = _vault_snapshot_from_samples(
                str(vault_addr).lower(),
                timeframe=timeframe_i,
                points=int(snapshot_points),
            )
            if snapshot is None and float(latest_usd_value or 0.0) > 0:
                last_usd = float(latest_usd_value or 0.0)
                snapshot = {
                    "timeframe": timeframe_i,
                    "tvl": [],
                    "stats": {"pctChange": 0.0, "lastUsd": last_usd, "min": last_usd, "max": last_usd},
                }
        items.append(
            {
                "id": str(vault_addr or "").lower(),
                "address": str(vault_addr or "").lower(),
                "owner": str(owner or "").lower(),
                "quoteAsset": str(quote or "").lower(),
                "baseAsset": str(base or "").lower(),
                "market": str(market or "").lower(),
                "name": name or "Vault",
                "desc": description or "",
                "social1": social1 or "",
                "social2": social2 or "",
                "social3": social3 or "",
                "type": "Spot",
                "locked": bool(locked),
                "closed": bool(closed),
                "lockup": int(lockup or 0),
                "decreaseOnWithdraw": bool(decrease_on_withdraw),
                "maxShares": str(int(max_shares or 0)),
                "totalShares": str(int(circulating_shares or 0)),
                "userShares": str(u_shares),
                "userSharePct": user_share_pct,
                "quoteBalance": str(int(latest_quote_balance or 0)),
                "baseBalance": str(int(latest_base_balance or 0)),
                "tvlUsd": float(latest_usd_value or 0.0),
                "quoteDecimals": int(quote_decimals or 0),
                "baseDecimals": int(base_decimals or 0),
                "quoteTicker": quote_ticker or "",
                "baseTicker": base_ticker or "",
                "quoteName": quote_name or "",
                "baseName": base_name or "",
                "lastDeposit": int(last_deposit or 0),
                "apyPct": row_apy[0] if row_apy else None,
                "apyWindowSecs": row_apy[1] if row_apy else None,
                "latestBalance": {
                    "block": int(latest_block or 0),
                    "timestamp": int(latest_ts or 0),
                    "quoteBalance": str(int(latest_quote_balance or 0)),
                    "baseBalance": str(int(latest_base_balance or 0)),
                    "usdValue": float(latest_usd_value or 0.0),
                },
                "snapshot": snapshot,
            }
        )

    page_i = int(page)
    limit_i = int(limit)
    return {
        "ok": True,
        "count": len(items),
        "total": total_count,
        "page": page_i,
        "limit": limit_i,
        "hasMore": (page_i * limit_i) < total_count,
        "filters": {
            "user": user_addr,
            "search": (search or ""),
            "status": status_norm,
            "sort": sort_norm,
            "order": order_norm,
            "includeSnapshot": bool(include_snapshot),
            "snapshotTimeframe": timeframe_i,
            "snapshotPoints": int(snapshot_points),
        },
        "vaults": items,
    }


@router.post("/vaults/{address}/refresh-balance")
def vault_refresh_balance(
    address: str,
    user: str | None = Query(None, description="optional user address for user balance projection"),
) -> dict[str, Any]:
    vaddr = (address or "").lower()
    v = storage.get_crystal_vault(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")
    (
        vault_addr,
        quote,
        base,
        market,
        owner,
        name,
        description,
        social1,
        social2,
        social3,
        locked,
        closed,
        max_shares,
        circulating_shares,
        quote_decimals,
        base_decimals,
        lockup,
        decrease_on_withdraw,
    ) = v

    try:
        head = _rpc_jsonrpc_sync("eth_blockNumber", [])
        blk_hex = head.get("result")
        if not isinstance(blk_hex, str) or not blk_hex.startswith("0x"):
            raise ValueError("invalid head block")
        blk_num = int(blk_hex, 16)

        ts = int(time.time())
        try:
            blk = _rpc_jsonrpc_sync("eth_getBlockByNumber", [blk_hex, False])
            ts_hex = (blk.get("result") or {}).get("timestamp") if isinstance(blk.get("result"), dict) else None
            if isinstance(ts_hex, str) and ts_hex.startswith("0x"):
                ts = int(ts_hex, 16)
        except Exception:
            pass

        call = _rpc_jsonrpc_sync(
            "eth_call",
            [{"to": vaddr, "data": "0x00113e08"}, blk_hex],
        )
        ret = call.get("result")
        if not isinstance(ret, str) or not ret.startswith("0x"):
            raise ValueError("invalid eth_call result")
        s = ret[2:].rjust(64 * 4, "0")
        quote_bal = int(s[0:64], 16)
        base_bal = int(s[64:128], 16)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"vault balance refresh failed: {e}")

    circ = int(circulating_shares or 0)
    uaddr = (user if isinstance(user, str) else "" or "").lower()
    usd_value = None
    try:
        st = _cached_state()
        computed = _vault_usd_from_state(st, quote, base, quote_bal, base_bal, quote_decimals, base_decimals)
        if computed > 0:
            usd_value = computed
    except Exception:
        usd_value = None
    if usd_value is None:
        latest_row = storage.get_crystal_vault_latest_balance(vaddr)
        if latest_row is not None:
            usd_value = float(latest_row[4] or 0.0)
    user_summary = None
    if uaddr:
        ur = storage.get_crystal_vault_user(vaddr, uaddr)
        u_shares = int(ur[0] or 0) if ur else 0
        share_pct = (u_shares / circ) if circ > 0 and u_shares > 0 else 0.0
        lock_fields = _vault_user_lockup_fields(
            now_ts=int(ts),
            lockup_seconds=int(lockup or 0),
            user_row=ur,
            user_shares=u_shares,
        )
        user_summary = {
            "address": uaddr,
            "shares": u_shares,
            "sharePct": share_pct,
            "quoteBalance": int(quote_bal * share_pct) if share_pct > 0 else 0,
            "baseBalance": int(base_bal * share_pct) if share_pct > 0 else 0,
            **lock_fields,
        }

    return {
        "ok": True,
        "vault": str(vault_addr or "").lower(),
        "totalShares": str(circ),
        "latestBalance": {
            "block": int(blk_num),
            "timestamp": int(ts),
            "quoteBalance": int(quote_bal),
            "baseBalance": int(base_bal),
            "usdValue": float(usd_value or 0.0),
        },
        "userBalance": user_summary,
        "status": {"locked": bool(locked), "closed": bool(closed)},
        "samplePersisted": False,
        "source": "rpc",
    }


@router.get("/vaults/{address}/{user}")
def vault_user_summary(
    address: str,
    user: str,
    history_limit: int = Query(50, ge=1, le=500),
    snapshot_timeframe: str = Query("1", description="1|2|3|4 or 1d|1w|1m|all-time"),
) -> dict[str, Any]:
    vaddr = address.lower()
    uaddr = user.lower()
    v = storage.get_crystal_vault(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")
    (
        vault_addr,
        quote,
        base,
        market,
        owner,
        name,
        description,
        social1,
        social2,
        social3,
        locked,
        closed,
        max_shares,
        circulating_shares,
        quote_decimals,
        base_decimals,
        lockup,
        decrease_on_withdraw,
    ) = v

    latest_row = storage.get_crystal_vault_latest_balance(vaddr)
    if latest_row:
        latest = {
            "block": int(latest_row[0] or 0),
            "timestamp": int(latest_row[1] or 0),
            "quoteBalance": int(latest_row[2] or 0),
            "baseBalance": int(latest_row[3] or 0),
            "usdValue": float(latest_row[4] or 0.0),
        }
        latest_shares = int(latest_row[5] or 0) if len(latest_row) > 5 else 0
    else:
        latest = {"quoteBalance": 0, "baseBalance": 0, "timestamp": 0, "usdValue": 0.0, "block": 0}
        latest_shares = 0

    nav_now = (latest["usdValue"] / latest_shares) if latest_shares > 0 and latest["usdValue"] > 0 else None

    circ = int(circulating_shares or 0)
    user_row = storage.get_crystal_vault_user(vaddr, uaddr)
    u_shares = int(user_row[0] or 0) if user_row else 0
    now_ts = int(time.time())
    lock_fields = _vault_user_lockup_fields(
        now_ts=now_ts,
        lockup_seconds=int(lockup or 0),
        user_row=user_row,
        user_shares=u_shares,
    )

    if circ > 0 and u_shares > 0:
        share_pct = u_shares / circ
        user_quote = int(latest["quoteBalance"] * share_pct)
        user_base = int(latest["baseBalance"] * share_pct)
    else:
        share_pct = 0.0
        user_quote = 0
        user_base = 0

    deps = storage.list_crystal_vault_deposits(vaddr, history_limit)
    wds = storage.list_crystal_vault_withdrawals(vaddr, history_limit)

    deposit_history = [
        {
            "user": str(d[0]).lower(),
            "timestamp": int(d[1] or 0),
            "quoteAmount": int(d[2] or 0),
            "baseAmount": int(d[3] or 0),
            "shares": int(d[4] or 0),
            "hash": d[5],
        }
        for d in deps
    ]
    withdraw_history = [
        {
            "user": str(w[0]).lower(),
            "timestamp": int(w[1] or 0),
            "quoteAmount": int(w[2] or 0),
            "baseAmount": int(w[3] or 0),
            "shares": int(w[4] or 0),
            "hash": w[5],
        }
        for w in wds
    ]

    depositors_raw = storage.list_crystal_vault_users(vaddr)
    depositors = []
    for d in depositors_raw:
        shares = int(d[1] or 0)
        depositors.append(
            {
                "address": str(d[0]).lower(),
                "shares": shares,
                "sharePct": (shares / circ) if circ > 0 else 0.0,
                "deposits": int(d[2] or 0),
                "withdraws": int(d[3] or 0),
                "lastDeposit": int(d[4] or 0),
                "lastWithdraw": int(d[5] or 0),
            }
        )

    timeframe_i = _parse_timeframe(snapshot_timeframe)
    snapshot = _vault_snapshot_from_samples(vaddr, timeframe=timeframe_i, points=0)
    if snapshot is None and float(latest["usdValue"] or 0.0) > 0:
        last_usd = float(latest["usdValue"] or 0.0)
        snapshot = {
            "timeframe": timeframe_i,
            "tvl": [],
            "stats": {
                "pctChange": 0.0,
                "lastUsd": last_usd,
                "min": last_usd,
                "max": last_usd,
            },
        }

    user_pnl = None
    try:
        user_pnl = _vault_user_pnl(vaddr, uaddr, circ, float(latest["usdValue"] or 0.0), nav_now=nav_now)
    except Exception:
        user_pnl = None

    apy_pct = None
    apy_window_secs = None
    try:
        apy_out = _vault_apy(vaddr)
        if apy_out:
            apy_pct, apy_window_secs = apy_out
    except Exception:
        apy_pct = None
        apy_window_secs = None

    return {
        "ok": True,
        "vault": {
            "vault": vault_addr,
            "owner": owner,
            "quote": quote,
            "base": base,
            "market": market,
            "name": name,
            "description": description,
            "socials": {"social1": social1, "social2": social2, "social3": social3},
            "decimals": {"quoteDecimals": int(quote_decimals or 0), "baseDecimals": int(base_decimals or 0)},
            "params": {
                "maxShares": int(max_shares or 0),
                "circulatingShares": int(circulating_shares or 0),
                "lockup": int(lockup or 0),
                "decreaseOnWithdraw": bool(decrease_on_withdraw),
            },
        },
        "status": {"locked": bool(locked), "closed": bool(closed)},
        "latestBalance": latest,
        "tvlUsd": latest["usdValue"],
        "apyPct": apy_pct,
        "apyWindowSecs": apy_window_secs,
        "userBalance": {
            "address": uaddr,
            "shares": u_shares,
            "sharePct": share_pct,
            "quoteBalance": user_quote,
            "baseBalance": user_base,
            "pnl": user_pnl,
            **lock_fields,
        },
        "depositHistory": deposit_history,
        "withdrawHistory": withdraw_history,
        "depositors": depositors,
        "snapshot": snapshot,
    }


@router.get("/vaults/{address}/history/{timeframe}")
def vault_history(
    address: str,
    timeframe: str,
    limit: int = Query(0, ge=0, le=2000),
) -> dict[str, Any]:
    vaddr = address.lower()
    v = storage.get_crystal_vault(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")
    timeframe_i = _parse_timeframe(timeframe)
    (
        vault_addr,
        quote,
        base,
        market,
        owner,
        name,
        description,
        social1,
        social2,
        social3,
        locked,
        closed,
        max_shares,
        circulating_shares,
        quote_decimals,
        base_decimals,
        lockup,
        decrease_on_withdraw,
    ) = v

    now_ts = int(time.time())
    if timeframe_i == 1:
        start_ts = now_ts - 86400
    elif timeframe_i == 2:
        start_ts = now_ts - (7 * 86400)
    elif timeframe_i == 3:
        start_ts = now_ts - (30 * 86400)
    else:
        start_ts = None

    pts_rows = storage.list_crystal_vault_balance_samples(vaddr, start_ts=start_ts, limit=limit)
    pts = [
        {
            "block": int(r[0] or 0),
            "timestamp": int(r[1] or 0),
            "quoteBalance": int(r[2] or 0),
            "baseBalance": int(r[3] or 0),
            "usdValue": float(r[4] or 0.0),
            "shares": int(r[5] or 0) if len(r) > 5 else 0,
        }
        for r in pts_rows
    ]
    raw_count = len(pts)

    tvl_full = [{"timestamp": int(p["timestamp"]), "tvlUsd": float(p["usdValue"])} for p in pts]
    pnl_full = _per_share_pnl_series(pts)
    tvl_series = _sample_evenly_by_time(tvl_full, 48, lambda p: int(p.get("timestamp") or 0))
    pnl_series = _sample_evenly_by_time(pnl_full, 48, lambda p: int(p.get("timestamp") or 0))

    tf_name = {1: "day", 2: "week", 3: "month", 4: "all"}[timeframe_i]
    return {
        "ok": True,
        "meta": {
            "vault": vault_addr,
            "name": name,
            "owner": owner,
            "quote": quote,
            "base": base,
            "locked": bool(locked),
            "closed": bool(closed),
            "circulatingShares": int(circulating_shares or 0),
        },
        "series": {"tvl": tvl_series, "pnl": pnl_series},
        "info": {"timeframe": tf_name, "count": len(tvl_series), "rawCount": raw_count},
    }
