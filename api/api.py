from __future__ import annotations
from decimal import Decimal
from dataclasses import asdict
from typing import Dict, Any, List, Deque, Tuple
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import time

from core.sequencer import SEQUENCER
from state import State
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
    marketcap_usd: Decimal = marketcap_native_raw * state.tokenToPrice.get("0x760afe86e5de5fa0ee542fc7b7b713e1c5425701", Decimal(0))

    tx_buy = lt.buy_count
    tx_sell = lt.sell_count
    tx_total = lt.tx_count or (tx_buy + tx_sell)
    
    dev_tokens_created = 0
    dev_tokens_graduated = 0
    if lt.creator:
        dev_user = state.launchpad_users.get(lt.creator.lower())
        if dev_user is not None:
            dev_tokens_created = dev_user.tokens_created
            dev_tokens_graduated = dev_user.tokens_graduated

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
        "marketcap_usd": str(marketcap_usd),
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
        "developer_tokens_created": dev_tokens_created,
        "developer_tokens_graduated": dev_tokens_graduated,
    }

def _bonding_curve_percent(marketcap_native_raw: Decimal) -> float:
    if marketcap_native_raw <= 0:
        return 0.0

    if marketcap_native_raw <= Decimal("1000"):
        return 0.0

    if marketcap_native_raw >= Decimal("25000"):
        return 100.0

    x = (marketcap_native_raw - Decimal("1000")) / (Decimal("25000") - Decimal("1000"))

    try:
        pct = (x ** Decimal("0.3910281574334157")) * Decimal(100)
    except Exception:
        return 0.0

    if pct < 0:
        pct = Decimal(0)
    if pct > 100:
        pct = Decimal(100)

    return float(pct)


@app.get("/tokens")
def list_tokens() -> Dict[str, List[Dict[str, Any]]]:
    state = SEQUENCER._state

    all_tokens = list(state.launchpad_tokens.values())

    graduated_tokens = [
        t for t in all_tokens if t.migrated
    ]
    recent_graduated = sorted(
        graduated_tokens,
        key=lambda t: (t.migrated_at or 0, t.migrated_block or 0),
        reverse=True,
    )[:30]

    graduated_ids = {t.token.lower() for t in recent_graduated}

    approaching_tokens = [
        t for t in all_tokens
        if t.approaching_75 and t.token.lower() not in graduated_ids
    ]
    recent_approaching = sorted(
        approaching_tokens,
        key=lambda t: (t.approaching_75_at or 0, t.approaching_75_block or 0),
        reverse=True,
    )[:30]

    approaching_ids = {t.token.lower() for t in recent_approaching}

    created_candidates = [
        t for t in all_tokens
        if (t.token.lower() not in graduated_ids)
        and (t.token.lower() not in approaching_ids)
    ]
    recent_created = sorted(
        created_candidates,
        key=lambda t: (t.created_at or 0, t.created_block or 0),
        reverse=True,
    )[:30]

    return {
        "recent_created": [_serialize_token(t.token) for t in recent_created],
        "recent_approaching": [_serialize_token(t.token) for t in recent_approaching],
        "recent_graduated": [_serialize_token(t.token) for t in recent_graduated],
    }

@app.get("/token/{token_addr}/{user_addr}/{timescale}/{page}")
def token_overview(
    token_addr: str,
    user_addr: str,
    timescale: int,
    page: int,
) -> Dict[str, Any]:
    state = SEQUENCER._state
    token_addr = token_addr.lower()
    user_addr = user_addr.lower()

    if timescale not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
        raise HTTPException(status_code=400, detail="unsupported timescale")

    if page < 1:
        page = 1

    lp = state.launchpad_tokens.get(token_addr)
    if lp is None:
        raise HTTPException(status_code=404, detail="launchpad token not found")

    trades = state.launchpad_trades.get(token_addr, [])
    now_ts = int(trades[-1].timestamp) if trades else int(lp.created_at or 0) or int(time.time())

    # klines
    candles: List[Dict[str, Any]] = []

    if trades:
        buckets: Dict[int, Dict[str, Any]] = {}

        dec_zero = Decimal(0)

        for tr in trades:
            ts_tr = int(tr.timestamp)
            bucket_start = (ts_tr // timescale) * timescale

            price = tr.price_native if tr.price_native is not None else dec_zero

            b = buckets.get(bucket_start)
            if b is None:
                b = {
                    "t": bucket_start,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "volume_native": 0,
                    "volume_token": 0,
                    "buy_native": 0,
                    "sell_native": 0,
                }
                buckets[bucket_start] = b
            else:
                b["c"] = price
                if price > b["h"]:
                    b["h"] = price
                if price < b["l"]:
                    b["l"] = price

            native_amt = int(tr.native_amount)
            token_amt = int(tr.token_amount)
            b["volume_native"] += native_amt
            b["volume_token"] += token_amt
            if tr.is_buy:
                b["buy_native"] += native_amt
            else:
                b["sell_native"] += native_amt

        bucket_times = sorted(buckets.keys())
        bucket_times = bucket_times[-5000:]

        for t_start in bucket_times:
            b = buckets[t_start]
            candles.append(
                {
                    "t": int(b["t"]),
                    "o": str(b["o"]),
                    "h": str(b["h"]),
                    "l": str(b["l"]),
                    "c": str(b["c"]),
                    "volume_native": str(b["volume_native"]),
                    "volume_token": str(b["volume_token"]),
                    "buy_native": str(b["buy_native"]),
                    "sell_native": str(b["sell_native"]),
                }
            )
    else:
        candles = []

    # header
    marketcap_native_raw: Decimal = lp.last_price_native * Decimal(1e9)
    mon_price = state.tokenToPrice.get("0x760afe86e5de5fa0ee542fc7b7b713e1c5425701", Decimal(0))
    marketcap_usd = marketcap_native_raw * mon_price if mon_price > 0 else Decimal(0)

    price_now = float(lp.last_price_native) if lp.last_price_native > 0 else 0.0
    cutoff_24h = now_ts - 24 * 3600
    price_before: float | None = None
    price_after: float | None = None

    for tr in trades:
        t_ts = int(tr.timestamp)
        p = float(tr.price_native) if tr.price_native is not None else 0.0
        if t_ts < cutoff_24h:
            price_before = p
        elif t_ts >= cutoff_24h:
            price_after = p
            break

    if price_after is not None:
        price_24h = price_after
    elif price_before is not None:
        price_24h = price_before
    else:
        price_24h = price_now

    if price_24h > 0:
        change_24h_pct = (price_now - price_24h) / price_24h * 100.0
    else:
        change_24h_pct = 0.0

    bonding_percent = _bonding_curve_percent(marketcap_native_raw)

    dev_tokens_migrated = 0
    dev_tokens_created = 0
    dev_addr = lp.creator.lower() if lp.creator else ""
    if dev_addr:
        dev_user = state.launchpad_users.get(dev_addr)
        if dev_user is not None:
            dev_tokens_migrated = dev_user.tokens_graduated
            dev_tokens_created = dev_user.tokens_created

    # recent trades
    recent_trades_raw = trades[-50:] if trades else []
    recent_trades = []
    for tr in reversed(recent_trades_raw):
        mc_trade_native = tr.price_native * Decimal(1e9) if tr.price_native is not None else Decimal(0)
        recent_trades.append(
            {
                "block": int(tr.block_number),
                "timestamp": int(tr.timestamp),
                "user": tr.user,
                "side": "buy" if tr.is_buy else "sell",
                "native_amount": str(tr.native_amount),
                "token_amount": str(tr.token_amount),
                "marketcap_native_raw": str(mc_trade_native),
            }
        )

    header = {
        "token": lp.token,
        "symbol": lp.symbol,
        "name": lp.name,
        "marketcap_native_raw": str(marketcap_native_raw),
        "marketcap_usd": str(marketcap_usd),
        "change_24h_pct": change_24h_pct,
        "bonding_curve_percent": bonding_percent,
        "developer": dev_addr,
        "developer_tokens_created": dev_tokens_created,
        "developer_tokens_migrated": dev_tokens_migrated,
        "recent_trades": recent_trades,
    }

    holders_count, dev_holding_tokens, top10_holding_tokens = _holders_for_token(lp.token)

    # 5m, 1h, 6h, 24h volume stats
    horizons = {
        "5m": 5 * 60,
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
    }

    dec_1e18 = Decimal(10) ** 18
    volume_windows: Dict[str, Dict[str, Any]] = {}

    if trades and mon_price > 0:
        for label, horizon in horizons.items():
            cutoff = now_ts - horizon
            buy_native = 0
            sell_native = 0

            for tr in reversed(trades):
                t_ts = int(tr.timestamp)
                if t_ts < cutoff:
                    break
                if tr.is_buy:
                    buy_native += int(tr.native_amount)
                else:
                    sell_native += int(tr.native_amount)

            net_native = buy_native - sell_native

            buy_native_dec = Decimal(buy_native) / dec_1e18
            sell_native_dec = Decimal(sell_native) / dec_1e18
            net_native_dec = Decimal(net_native) / dec_1e18

            buy_usd = buy_native_dec * mon_price
            sell_usd = sell_native_dec * mon_price
            net_usd = net_native_dec * mon_price

            volume_windows[label] = {
                "buy_native": str(buy_native),
                "sell_native": str(sell_native),
                "net_native": str(net_native),
                "buy_usd": str(buy_usd),
                "sell_usd": str(sell_usd),
                "net_usd": str(net_usd),
            }
    else:
        for label in horizons.keys():
            volume_windows[label] = {
                "buy_native": "0",
                "sell_native": "0",
                "net_native": "0",
                "buy_usd": "0",
                "sell_usd": "0",
                "net_usd": "0",
            }

    # current user per token stats
    pos = state.launchpad_positions.get((user_addr, token_addr))
    if pos is not None:
        user_native_spent = Decimal(pos.native_spent)
        user_native_received = Decimal(pos.native_received)
        user_realized_pnl = pos.realized_pnl_native
        current_value_native = Decimal(pos.balance_token) * lp.last_price_native
        user_token_balance = pos.balance_token
        user_token_bought = pos.token_bought
        user_token_sold = pos.token_sold
    else:
        user_native_spent = Decimal(0)
        user_native_received = Decimal(0)
        user_realized_pnl = Decimal(0)
        current_value_native = Decimal(0)
        user_token_balance = 0
        user_token_bought = 0
        user_token_sold = 0

    token_info = {
        "token": lp.token,
        "metadataCID": lp.metadata_cid,
        "developer": dev_addr,
        "holders": holders_count,
        "developer_holding_tokens": str(dev_holding_tokens),
        "top10_holding_tokens": str(top10_holding_tokens),
        "volume_windows": volume_windows,
        "user": {
            "address": user_addr,
            "native_spent": str(user_native_spent),
            "native_received": str(user_native_received),
            "native_realized_pnl": str(user_realized_pnl),
            "token_bought": str(user_token_bought),
            "token_sold": str(user_token_sold),
            "token_balance": str(user_token_balance),
            "current_balance_native": str(current_value_native),
        },
    }

    # similar tokens
    def _name_similarity(a: str, b: str) -> int:
        a = (a or "").lower()
        b = (b or "").lower()
        score = 0
        for ca, cb in zip(a, b):
            if ca == cb:
                score += 1
            else:
                break
        return score

    base_name = lp.name or ""
    similar_candidates: List[tuple[int, models.LaunchpadToken]] = []
    for other in state.launchpad_tokens.values():
        if other.token == token_addr:
            continue
        score = _name_similarity(base_name, other.name)
        if score > 0:
            similar_candidates.append((score, other))

    similar_candidates.sort(key=lambda x: x[0], reverse=True)
    similar_candidates = similar_candidates[:5]

    similar_tokens: List[Dict[str, Any]] = []
    for score, other in similar_candidates:
        other_trades = state.launchpad_trades.get(other.token, [])
        if other_trades:
            last_tx_ts = int(other_trades[-1].timestamp)
        else:
            last_tx_ts = int(other.created_at or 0)
        seconds_since_last_tx = max(0, now_ts - last_tx_ts)

        other_mc_native = other.last_price_native * Decimal(1e9)
        other_mc_usd = other_mc_native * mon_price if mon_price > 0 else Decimal(0)

        other_trades_local = other_trades
        cutoff = now_ts - 24 * 3600
        other_buy_native = 0
        other_sell_native = 0
        for tr in reversed(other_trades_local):
            t_ts = int(tr.timestamp)
            if t_ts < cutoff:
                break
            if tr.is_buy:
                other_buy_native += int(tr.native_amount)
            else:
                other_sell_native += int(tr.native_amount)
        other_net_native = other_buy_native + other_sell_native

        other_buy_native_dec = Decimal(other_buy_native) / dec_1e18
        other_sell_native_dec = Decimal(other_sell_native) / dec_1e18
        other_net_native_dec = Decimal(other_net_native) / dec_1e18

        other_buy_usd = other_buy_native_dec * mon_price
        other_sell_usd = other_sell_native_dec * mon_price
        other_net_usd = other_net_native_dec * mon_price

        similar_tokens.append(
            {
                "token": other.token,
                "symbol": other.symbol,
                "name": other.name,
                "metadata_cid": other.metadata_cid,
                "seconds_since_last_tx": seconds_since_last_tx,
                "marketcap_native_raw": str(other_mc_native),
                "marketcap_usd": str(other_mc_usd),
                "volume_24h": {
                    "buy_native": str(other_buy_native),
                    "sell_native": str(other_sell_native),
                    "net_native": str(other_net_native),
                    "buy_usd": str(other_buy_usd),
                    "sell_usd": str(other_sell_usd),
                    "net_usd": str(other_net_usd),
                },
            }
        )

    token_info["similar_tokens"] = similar_tokens

    # ordercenter
    user_portfolio: List[Dict[str, Any]] = []
    for (uaddr, tkn), pos_any in state.launchpad_positions.items():
        if uaddr != user_addr:
            continue
        lp_tkn = state.launchpad_tokens.get(tkn)
        if lp_tkn is None:
            continue
        value_native = Decimal(pos_any.balance_token) * lp_tkn.last_price_native
        user_portfolio.append(
            {
                "token": tkn,
                "symbol": lp_tkn.symbol,
                "native_spent": str(pos_any.native_spent),
                "native_received": str(pos_any.native_received),
                "token_bought": str(pos_any.token_bought),
                "token_sold": str(pos_any.token_sold),
                "token_balance": str(pos_any.balance_token),
                "current_balance_native": str(value_native),
                "realized_pnl_native": str(pos_any.realized_pnl_native),
            }
        )

    token_positions = [
        pos_any
        for (uaddr, tkn), pos_any in state.launchpad_positions.items()
        if tkn == token_addr
    ]
    token_positions.sort(key=lambda p: p.balance_token, reverse=True)
    top_holders_raw = token_positions[:50]

    top_holders: List[Dict[str, Any]] = []
    for pos_any in top_holders_raw:
        value_native = Decimal(pos_any.balance_token) * lp.last_price_native
        top_holders.append(
            {
                "user": pos_any.user,
                "token_bought": str(pos_any.token_bought),
                "token_sold": str(pos_any.token_sold),
                "token_balance": str(pos_any.balance_token),
                "native_spent": str(pos_any.native_spent),
                "native_received": str(pos_any.native_received),
                "current_balance_native": str(value_native),
                "realized_pnl_native": str(pos_any.realized_pnl_native),
            }
        )

    token_positions_by_pnl = list(token_positions)
    token_positions_by_pnl.sort(key=lambda p: p.realized_pnl_native, reverse=True)
    top_traders_raw = token_positions_by_pnl[:50]

    top_traders: List[Dict[str, Any]] = []
    for pos_any in top_traders_raw:
        value_native = Decimal(pos_any.balance_token) * lp.last_price_native
        top_traders.append(
            {
                "user": pos_any.user,
                "token_bought": str(pos_any.token_bought),
                "token_sold": str(pos_any.token_sold),
                "token_balance": str(pos_any.balance_token),
                "native_spent": str(pos_any.native_spent),
                "native_received": str(pos_any.native_received),
                "current_balance_native": str(value_native),
                "realized_pnl_native": str(pos_any.realized_pnl_native),
            }
        )

    dev_tokens = []
    if dev_addr:
        for t in state.launchpad_tokens.values():
            if (t.creator or "").lower() == dev_addr:
                dev_tokens.append(t)

    dev_tokens.sort(key=lambda t: (int(t.created_at or 0), int(t.created_block or 0)), reverse=True)
    recent_dev_tokens_raw = dev_tokens[:50]

    recent_dev_tokens: List[Dict[str, Any]] = []
    dev_highest_mc_token = None
    dev_highest_mc_value = Decimal(0)
    latest_launch_ts = 0

    for t in dev_tokens:
        mc_native = t.last_price_native * Decimal(1e9)
        if mc_native > dev_highest_mc_value:
            dev_highest_mc_value = mc_native
            dev_highest_mc_token = t.token
        if int(t.created_at or 0) > latest_launch_ts:
            latest_launch_ts = int(t.created_at or 0)

    for t in recent_dev_tokens_raw:
        mc_native = t.last_price_native * Decimal(1e9)
        holders_count_t, _, _ = _holders_for_token(t.token)
        recent_dev_tokens.append(
            {
                "token": t.token,
                "symbol": t.symbol,
                "name": t.name,
                "metadata_cid": t.metadata_cid,
                "created_at": int(t.created_at or 0),
                "marketcap_native_raw": str(mc_native),
                "migrated": t.migrated,
                "holders": holders_count_t,
            }
        )

    ordercenter = {
        "user_portfolio": user_portfolio,
        "top_holders": top_holders,
        "top_traders": top_traders,
        "dev_tokens_recent": recent_dev_tokens,
        "dev_summary": {
            "developer": dev_addr,
            "tokens_created": dev_tokens_created,
            "tokens_migrated": dev_tokens_migrated,
            "highest_marketcap_token": dev_highest_mc_token,
            "highest_marketcap_native": str(dev_highest_mc_value),
            "latest_launch_timestamp": latest_launch_ts,
            "total_tokens_launched": len(dev_tokens),
        },
    }

    return {
        "token": lp.token,
        "user": user_addr,
        "timescale": timescale,
        "page": page,
        "candles": candles,
        "header": header,
        "token_info": token_info,
        "ordercenter": ordercenter,
    }
