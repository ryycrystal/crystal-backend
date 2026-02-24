from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, Query, HTTPException
from api.api import storage, time, _vault_snapshot_from_samples, ttl_cache

router = APIRouter()

# endpoint for vault list used by /earn/vaults
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
    snapshot_timeframe: int = Query(1, ge=1, le=4),
    snapshot_points: int = Query(48, ge=0, le=200),
) -> Dict[str, Any]:
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
        snapshot = None
        if include_snapshot:
            snapshot = _vault_snapshot_from_samples(
                str(vault_addr).lower(),
                timeframe=int(snapshot_timeframe),
                points=int(snapshot_points),
            )
            if snapshot is None and float(latest_usd_value or 0.0) > 0:
                last_usd = float(latest_usd_value or 0.0)
                snapshot = {
                    "timeframe": int(snapshot_timeframe),
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
            "snapshotTimeframe": int(snapshot_timeframe),
            "snapshotPoints": int(snapshot_points),
        },
        "vaults": items,
    }

# endpoint for complete vaults/{address} page
@router.get("/vaults/{address}/{user}")
def vault_user_summary(
    address: str,
    user: str,
    history_limit: int = Query(50, ge=1, le=500),
    snapshot_timeframe: int = Query(1, ge=1, le=4),
) -> Dict[str, Any]:
    vaddr = address.lower()
    uaddr = user.lower()
    v = storage.get_crystal_vault(vaddr)
    if not v:
        return {"ok": False, "error": "vault not found", "vault": vaddr}
    (
        vault_addr, quote, base, market, owner, name, description, social1, social2, social3,
        locked, closed, max_shares, circulating_shares, quote_decimals, base_decimals,
        lockup, decrease_on_withdraw,
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
    else:
        latest = {"quoteBalance": 0, "baseBalance": 0, "timestamp": 0, "usdValue": 0.0, "block": 0}

    circ = int(circulating_shares or 0)
    user_row = storage.get_crystal_vault_user(vaddr, uaddr)
    u_shares = int(user_row[0] or 0) if user_row else 0

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
        depositors.append({
            "address": str(d[0]).lower(),
            "shares": shares,
            "sharePct": (shares / circ) if circ > 0 else 0.0,
            "deposits": int(d[2] or 0),
            "withdraws": int(d[3] or 0),
            "lastDeposit": int(d[4] or 0),
            "lastWithdraw": int(d[5] or 0),
        })

    snapshot = _vault_snapshot_from_samples(vaddr, timeframe=int(snapshot_timeframe), points=0)
    if snapshot is None and float(latest["usdValue"] or 0.0) > 0:
        last_usd = float(latest["usdValue"] or 0.0)
        snapshot = {
            "timeframe": int(snapshot_timeframe),
            "tvl": [],
            "stats": {
                "pctChange": 0.0,
                "lastUsd": last_usd,
                "min": last_usd,
                "max": last_usd,
            },
        }

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
        "userBalance": {
            "address": uaddr,
            "shares": u_shares,
            "sharePct": share_pct,
            "quoteBalance": user_quote,
            "baseBalance": user_base,
        },
        "depositHistory": deposit_history,
        "withdrawHistory": withdraw_history,
        "depositors": depositors,
        "snapshot": snapshot,
    }

# endpoint for vault history charts (used when timeframe is changed while staying on the same vault)
@router.get("/vaults/{address}/history/{timeframe}")
def vault_history(
    address: str,
    timeframe: int,
    limit: int = Query(0, ge=0, le=2000),
) -> Dict[str, Any]:
    vaddr = address.lower()
    v = storage.get_crystal_vault(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")
    if timeframe not in {1, 2, 3, 4}:
        raise HTTPException(status_code=400, detail="invalid timeframe")
    (
        vault_addr, quote, base, market, owner, name, description, social1, social2, social3,
        locked, closed, max_shares, circulating_shares, quote_decimals, base_decimals,
        lockup, decrease_on_withdraw,
    ) = v

    now_ts = int(time.time())
    if timeframe == 1:
        start_ts = now_ts - 86400
    elif timeframe == 2:
        start_ts = now_ts - (7 * 86400)
    elif timeframe == 3:
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
        }
        for r in pts_rows
    ]

    tvl_series = [{"timestamp": int(p["timestamp"]), "tvlUsd": float(p["usdValue"])} for p in pts]
    base_usd = float(pts[0]["usdValue"]) if pts else 0.0
    pnl_series = []
    for p in pts:
        cur_usd = float(p["usdValue"])
        pnl_usd = cur_usd - base_usd
        pnl_pct = ((cur_usd / base_usd) - 1.0) * 100.0 if base_usd > 0 else 0.0
        pnl_series.append({"timestamp": int(p["timestamp"]), "pnlUsd": pnl_usd, "pnlPct": pnl_pct})

    tf_name = {1: "day", 2: "week", 3: "month", 4: "all"}[timeframe]
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
        "info": {"timeframe": tf_name, "count": len(pts)},
    }
