from __future__ import annotations
from decimal import Decimal
from dataclasses import asdict
from typing import Dict, Any, List, Deque, Tuple
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import time

from core.sequencer import SEQUENCER
from state import INTERVALS, LABEL
from api.x_api import router as x_router
import models

_TIMEFRAME_SEC = {
    1: 86400,
    2: 7 * 86400,
    3: 30 * 86400,
    4: 10 * 365 * 86400,
}

def _pick_effective_step(age_sec: int, requested_window: int) -> int:
    window = min(max(0, age_sec), max(0, requested_window))
    if window >= 30 * 86400 and age_sec >= 30 * 86400:
        return 86400
    if window >= 7 * 86400:
        return 21600
    if window >= 86400:
        return 3600
    return 900

def _row_to_dict(b: Any) -> Dict[str, Any]:
    if isinstance(b, dict):
        return {
            "block": int(b.get("block", 0)),
            "timestamp": int(b.get("timestamp", 0)),
            "quoteBalance": int(b.get("quoteBalance", 0)),
            "baseBalance": int(b.get("baseBalance", 0)),
            "usdValue": float(b.get("usdValue", 0.0)),
        }
    return {
        "block": int(getattr(b, "block", 0)),
        "timestamp": int(getattr(b, "timestamp", 0)),
        "quoteBalance": int(getattr(b, "quoteBalance", 0)),
        "baseBalance": int(getattr(b, "baseBalance", 0)),
        "usdValue": float(getattr(b, "usdValue", 0.0)),
    }

def _pick_bucket(vaddr: str) -> Dict[int, Deque[models.VaultBalance]]:
    st = SEQUENCER._state
    return {
        900: st.vaultBalancesDay.get(vaddr, None),
        3600: st.vaultBalancesWeek.get(vaddr, None),
        21600: st.vaultBalancesMonth.get(vaddr, None),
        86400: st.vaultBalancesAllTime.get(vaddr, None),
    }

def _latest_balance_for(vaddr: str) -> Dict[str, Any]:
    dq = SEQUENCER._state.vaultBalancesAllTime.get(vaddr)
    if not dq:
        return {"quoteBalance": 0, "baseBalance": 0, "timestamp": 0, "usdValue": 0.0}
    row = _row_to_dict(dq[-1])
    return {
        "quoteBalance": int(row["quoteBalance"]),
        "baseBalance": int(row["baseBalance"]),
        "timestamp": int(row["timestamp"]),
        "usdValue": float(row["usdValue"]),
    }

def _build_history(v: models.Vault, timeframe: int, limit: int) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    if timeframe not in _TIMEFRAME_SEC:
        raise HTTPException(status_code=400, detail="invalid timeframe")

    now = int(time.time())
    created_ts = int(getattr(v, "timestamp", 0)) or 0
    age = max(0, now - created_ts)

    requested_window = _TIMEFRAME_SEC[timeframe]
    step = _pick_effective_step(age, requested_window)

    buckets = _pick_bucket(v.vault.lower())
    dq = buckets.get(step)

    if (not dq) or len(dq) == 0:
        for fallback in [3600, 21600, 900, 86400]:
            if fallback == step:
                continue
            cand = buckets.get(fallback)
            if cand and len(cand) > 0:
                dq = cand
                step = fallback
                break

    if not dq or len(dq) == 0:
        return (
            {"step": step, "count": 0, "effectiveWindowSec": 0, "requestedWindowSec": requested_window, "ageSec": age},
            {"tvl": [], "pnl": []},
        )

    pts_raw = list(dq)[-limit:] if limit and limit > 0 else list(dq)
    pts = [_row_to_dict(b) for b in pts_raw]

    tvl_series = [{"timestamp": p["timestamp"], "tvlUsd": p["usdValue"]} for p in pts]

    base_usd = pts[0]["usdValue"] if pts else 0.0
    pnl_series: List[Dict[str, Any]] = []
    for p in pts:
        cur = p["usdValue"]
        pnl_usd = cur - base_usd
        pnl_pct = ((cur / base_usd) - 1.0) * 100.0 if base_usd > 0 else 0.0
        pnl_series.append({"timestamp": p["timestamp"], "pnlUsd": pnl_usd, "pnlPct": pnl_pct})

    effective_window = pts[-1]["timestamp"] - pts[0]["timestamp"] if len(pts) >= 2 else 0

    info = {
        "step": step,
        "count": len(pts),
        "effectiveWindowSec": effective_window,
        "requestedWindowSec": requested_window,
        "ageSec": age,
    }
    series = {"tvl": tvl_series, "pnl": pnl_series}
    return info, series

app = FastAPI(title="backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(x_router)

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/debug/token-to-price")
def token_to_price(as_strings: bool = Query(False)) -> Dict[str, Any]:
    st = SEQUENCER._state
    out: Dict[str, Any] = {}

    for addr, decv in st.tokenToPrice.items():
        try:
            out[addr.lower()] = str(decv) if as_strings else float(decv)
        except Exception:
            out[addr.lower()] = str(decv)

    return {
        "ok": True,
        "count": len(out),
        "prices": out,
    }

@app.get("/debug/markets")
def debug_markets() -> Dict[str, Any]:
    st = SEQUENCER._state

    def serialize_market(mi: Any) -> Dict[str, Any]:
        if isinstance(mi, dict):
            out: Dict[str, Any] = {}
            for k, v in mi.items():
                if isinstance(v, Decimal):
                    out[k] = float(v)
                else:
                    out[k] = v
            return out

        try:
            data = asdict(mi)
        except TypeError:
            data = {
                k: getattr(mi, k)
                for k in dir(mi)
                if not k.startswith("_") and not callable(getattr(mi, k))
            }

        for k, v in list(data.items()):
            if isinstance(v, Decimal):
                data[k] = float(v)

        return data

    token_graph_out: Dict[str, List[Dict[str, Any]]] = {}
    for token, markets in st.tokenGraph.items():
        token_graph_out[token.lower()] = [serialize_market(m) for m in markets]

    markets_out: Dict[str, Dict[str, Any]] = {}
    for addr, mi in st.addressToMarket.items():
        markets_out[addr.lower()] = serialize_market(mi)

    return {
        "ok": True,
        "tokenGraph": token_graph_out,
        "markets": markets_out,
    }


@app.get("/vaults/{address}/{user}/{timeframe}")
def vault_combined(
    address: str,
    user: str,
    timeframe: int,
    limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    vaddr = address.lower()
    uaddr = user.lower()

    st = SEQUENCER._state
    v = st.vaults.get(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")

    meta = {
        "vault": v.vault,
        "owner": v.owner,
        "quote": v.quote,
        "base": v.base,
        "market": v.market,
        "name": v.name,
        "description": v.description,
        "socials": {"social1": v.social1, "social2": v.social2, "social3": v.social3},
        "decimals": {"quoteDecimals": int(v.quoteDecimals), "baseDecimals": int(v.baseDecimals)},
        "params": {"maxShares": int(v.maxShares), "circulatingShares": int(v.circulatingShares)},
        "timestamp": int(getattr(v, "timestamp", 0) or 0),
    }
    status = {"locked": bool(v.locked), "closed": bool(v.closed)}

    latest = _latest_balance_for(vaddr)

    circ = int(v.circulatingShares) if v.circulatingShares is not None else 0
    uobj = st.vaultToUsers.get(vaddr, {}).get(uaddr)
    u_shares = int(uobj.shares) if uobj else 0
    if circ > 0 and u_shares > 0:
        share_pct = u_shares / circ
        user_quote = int(latest["quoteBalance"] * share_pct)
        user_base = int(latest["baseBalance"] * share_pct)
    else:
        share_pct = 0.0
        user_quote = 0
        user_base = 0
    user_balance = {"address": uaddr, "shares": u_shares, "sharePct": share_pct, "quoteBalance": user_quote, "baseBalance": user_base}

    deps = st.vaultToDeposits.get(vaddr, [])
    wds = st.vaultToWithdraws.get(vaddr, [])

    dep_hist = [{
        "user": d.user,
        "timestamp": int(d.timestamp),
        "quoteAmount": int(d.quoteAmount),
        "baseAmount": int(d.baseAmount),
        "shares": int(d.shares),
        "hash": d.hash,
    } for d in reversed(deps[-limit:])]

    wdr_hist = [{
        "user": w.user,
        "timestamp": int(w.timestamp),
        "quoteAmount": int(w.quoteAmount),
        "baseAmount": int(w.baseAmount),
        "shares": int(w.shares),
        "hash": w.hash,
    } for w in reversed(wds[-limit:])]

    depos_map = st.vaultToUsers.get(vaddr, {})
    circ_safe = circ if circ > 0 else 1
    depos_list = sorted(depos_map.values(), key=lambda x: int(x.lastDeposit or 0), reverse=True)
    depos_out = [{
        "address": d.address,
        "shares": int(d.shares),
        "sharePct": (d.shares / circ_safe),
        "deposits": int(d.deposits),
        "withdraws": int(d.withdraws),
        "lastDeposit": int(d.lastDeposit or 0),
        "lastWithdraw": int(d.lastWithdraw or 0),
    } for d in depos_list]

    info, series = _build_history(v, timeframe, limit)

    return {
        "ok": True,
        "vault": meta,
        "status": status,
        "latestBalance": latest,
        "tvlUsd": latest["usdValue"],
        "userBalance": user_balance,
        "depositHistory": dep_hist,
        "withdrawHistory": wdr_hist,
        "depositors": depos_out,
        "history": {
            "info": info,
            "series": series,
        },
    }

@app.get("/vaults/list")
def list_vaults(
    limit: int = Query(100, ge=1, le=1000),
    timeframe: int = Query(2, ge=1, le=4),
    hist_limit: int = Query(40, ge=8, le=200),
    include_snapshots: bool = Query(True),
) -> Dict[str, Any]:
    st = SEQUENCER._state

    rows: List[Dict[str, Any]] = []
    for vaddr, v in st.vaults.items():
        latest = _latest_balance_for(vaddr)

        base_row = {
            "vault": v.vault,
            "name": v.name,
            "owner": v.owner,
            "quote": v.quote,
            "base": v.base,
            "locked": bool(v.locked),
            "closed": bool(v.closed),
            "circulatingShares": int(v.circulatingShares or 0),
            "quoteDecimals": int(v.quoteDecimals or 0),
            "baseDecimals": int(v.baseDecimals or 0),
            "timestamp": int(getattr(v, "timestamp", 0) or 0),
            "latest": latest,
            "tvlUsd": latest["usdValue"],
        }

        if include_snapshots:
            info, series = _build_history(v, timeframe, hist_limit)
            tvl = series.get("tvl", [])

            if tvl:
                vals = [float(p.get("tvlUsd", 0.0)) for p in tvl]
                last_usd = vals[-1]
                base = vals[0] if vals[0] > 0 else 0.0
                pct = ((last_usd / base) - 1.0) * 100.0 if base > 0 else 0.0
                compact = [[int(p["timestamp"]), float(p["tvlUsd"])] for p in tvl]
                snap = {
                    "ok": True,
                    "timeframe": timeframe,
                    "info": info,
                    "tvl": compact,
                    "stats": {
                        "lastUsd": last_usd,
                        "pctChange": pct,
                        "min": min(vals),
                        "max": max(vals),
                    },
                }
            else:
                snap = {
                    "ok": True,
                    "timeframe": timeframe,
                    "info": info,
                    "tvl": [],
                    "stats": {"lastUsd": 0.0, "pctChange": 0.0, "min": 0.0, "max": 0.0},
                }

            base_row["snapshot"] = snap

        rows.append(base_row)

    rows.sort(key=lambda r: (r["tvlUsd"], r["latest"]["timestamp"]), reverse=True)

    return {
        "ok": True,
        "count": min(limit, len(rows)),
        "timeframe": timeframe,
        "histLimit": hist_limit,
        "vaults": rows[:limit],
    }

@app.get("/vaults/{address}/{timeframe}")
def vault_chart_only(
    address: str,
    timeframe: int,
    limit: int = Query(400, ge=1, le=2000),
) -> Dict[str, Any]:
    vaddr = address.lower()
    st = SEQUENCER._state
    v = st.vaults.get(vaddr)
    if not v:
        raise HTTPException(status_code=404, detail="vault not found")

    info, series = _build_history(v, timeframe, limit)
    return {
        "ok": True,
        "timeframe": timeframe,
        "history": {
            "info": info,
            "series": series,
        },
    }
    
    
@app.get("/pools/list")
def list_pools():
    state = SEQUENCER._state

    items = []
    for pool in state.ammPools.values():
        items.append({
            "market": pool.market,
            "quote": pool.quote,
            "base": pool.base,
            "marketType": int(pool.marketType),
            "quoteDecimals": int(pool.quoteDecimals),
            "baseDecimals": int(pool.baseDecimals),
            "quoteTicker": pool.quoteTicker,
            "quoteName": pool.quoteName,
            "baseTicker": pool.baseTicker,
            "baseName": pool.baseName,
            "feeBps": int(pool.feeBps),
            "tvlUsd": float(pool.tvlUsd),
            "volume24hUsd": float(pool.volume24hUsd),
            "fees24hUsd": float(pool.fees24hUsd),
            "apy24h": float(pool.apy24h),
        })

    return {"pools": items}

@app.get("/pools/{address}")
def get_pool(address: str):
    state = SEQUENCER._state
    maddr = address.lower()

    pool = state.ammPools.get(maddr)
    if pool is None:
        raise HTTPException(status_code=404, detail="pool not found")

    tvl = float(pool.tvlUsd)
    fees_24h = float(pool.fees24hUsd)
    daily_yield = fees_24h / tvl if tvl > 0.0 else 0.0
    daily_yield_percent = daily_yield * 100.0
    apy_percent = float(pool.apy24h) * 100.0

    history = state.ammHistory.get(maddr, [])

    if len(history) > 200:
        history = history[-200:]

    return {
        "market": pool.market,
        "quote": pool.quote,
        "base": pool.base,
        "marketType": int(pool.marketType),
        "quoteDecimals": int(pool.quoteDecimals),
        "baseDecimals": int(pool.baseDecimals),
        "quoteTicker": pool.quoteTicker,
        "quoteName": pool.quoteName,
        "baseTicker": pool.baseTicker,
        "baseName": pool.baseName,
        "feeBps": int(pool.feeBps),
        "reserveQuote": str(pool.reserveQuote),
        "reserveBase": str(pool.reserveBase),
        "tvlUsd": tvl,
        "volume24hUsd": float(pool.volume24hUsd),
        "fees24hUsd": fees_24h,
        "dailyYield": daily_yield,
        "dailyYieldPercent": daily_yield_percent,
        "apy24h": float(pool.apy24h),
        "apy24hPercent": apy_percent,
        "apyHistory": history,
    }


def _holders_for_token(token_addr: str) -> Tuple[int, int, int]:
    state = SEQUENCER._state
    token_addr = token_addr.lower()

    pos_list = [
        pos
        for (user, tkn), pos in state.launchpad_positions.items()
        if tkn == token_addr and pos.balance_token > 0
    ]
    holder_count = len(pos_list)
    pos_list.sort(key=lambda p: p.balance_token, reverse=True)
    top10_holding = sum(p.balance_token for p in pos_list[:10])

    dev_holding = 0
    lt = state.launchpad_tokens.get(token_addr)
    if lt is not None and lt.creator:
        dev_pos = state.launchpad_positions.get((lt.creator.lower(), token_addr))
        if dev_pos:
            dev_holding = dev_pos.balance_token

    return holder_count, dev_holding, top10_holding

def _serialize_token(token_addr: str) -> Dict[str, Any]:
    state = SEQUENCER._state
    lt = state.launchpad_tokens.get(token_addr.lower())
    if lt is None:
        return {}

    holders, dev_holding, top10_holding = _holders_for_token(lt.token)

    marketcap_native_raw: Decimal = lt.last_price_native * Decimal(1e9)

    tx_buy = lt.buy_count
    tx_sell = lt.sell_count
    tx_total = lt.tx_count or (tx_buy + tx_sell)

    return {
        "token": lt.token,
        "symbol": lt.symbol,
        "name": lt.name,
        "created_ts": lt.created_at,
        "creator": lt.creator,
        "metadata_cid": lt.metadata_cid,
        "source": lt.source,
        "holders": holders,
        "developer_holding": str(dev_holding),
        "top10_holding": str(top10_holding),
        "native_volume": str(lt.native_volume),
        "token_volume": str(lt.token_volume),
        "volume_usd": str(lt.volume_usd),
        "fees_usd": str(lt.fees_usd),
        "marketcap_native_raw": str(marketcap_native_raw),
        "tx": {
            "buy": tx_buy,
            "sell": tx_sell,
            "total": tx_total,
        },

        "migrated": lt.migrated,
        "migrated_block": lt.migrated_block,
        "migrated_at": lt.migrated_at,

        "approaching_75": lt.approaching_75,
        "approaching_75_block": lt.approaching_75_block,
        "approaching_75_at": lt.approaching_75_at,
    }

@app.get("/tokens")
def list_tokens() -> Dict[str, List[Dict[str, Any]]]:
    state = SEQUENCER._state

    all_tokens = list(state.launchpad_tokens.values())

    recent_created = sorted(
        all_tokens,
        key=lambda t: (t.created_at or 0, t.created_block or 0),
        reverse=True,
    )[:30]

    approaching = [t for t in all_tokens if t.approaching_75]
    recent_approaching = sorted(
        approaching,
        key=lambda t: (t.approaching_75_at or 0, t.approaching_75_block or 0),
        reverse=True,
    )[:30]

    graduated = [t for t in all_tokens if t.migrated]
    recent_graduated = sorted(
        graduated,
        key=lambda t: (t.migrated_at or 0, t.migrated_block or 0),
        reverse=True,
    )[:30]

    return {
        "recent_created": [_serialize_token(t.token) for t in recent_created],
        "recent_approaching": [_serialize_token(t.token) for t in recent_approaching],
        "recent_graduated": [_serialize_token(t.token) for t in recent_graduated],
    }