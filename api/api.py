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
        and user.lower() != "0xad720f94689edb929d9be7613223320a0b2f260f"
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

def _build_ohlcv(
    trades: List[models.LaunchpadTrade],
    bucket_seconds: int,
    max_buckets: int | None = None,
) -> List[Dict[str, Any]]:
    if bucket_seconds <= 0 or not trades:
        return []

    buckets: Dict[int, Dict[str, Any]] = {}

    for tr in trades:
        ts_tr = int(tr.timestamp)
        bucket_start = (ts_tr // bucket_seconds) * bucket_seconds

        price_wad = tr.price_native * Decimal(1e9)
        token_amt = int(tr.token_amount)

        b = buckets.get(bucket_start)
        if b is None:
            b = {
                "time": bucket_start,
                "open": price_wad,
                "high": price_wad,
                "low": price_wad,
                "close": price_wad,
                "baseVolume": token_amt,
            }
            buckets[bucket_start] = b
        else:
            b["close"] = price_wad
            if price_wad > b["high"]:
                b["high"] = price_wad
            if price_wad < b["low"]:
                b["low"] = price_wad
            b["baseVolume"] += token_amt

    bucket_times = sorted(buckets.keys())
    if max_buckets is not None and max_buckets > 0:
        bucket_times = bucket_times[-max_buckets:]

    out: List[Dict[str, Any]] = []
    for t_start in bucket_times:
        b = buckets[t_start]
        out.append(
            {
                "time": str(int(b["time"])),
                "open": str(int(b["open"])),
                "high": str(int(b["high"])),
                "low": str(int(b["low"])),
                "close": str(int(b["close"])),
                "baseVolume": str(int(b["baseVolume"])),
            }
        )
    return out

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

@app.get("/token/{token_addr}/{chartres}")
def token_overview_graph(
    token_addr: str,
    chartres: int,
    tracked: str = Query(
        "",
        description="comma-separated list of addresses to track for trackedtrades",
    ),
) -> Dict[str, Any]:
    state = SEQUENCER._state
    token_addr = token_addr.lower()

    if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
        raise HTTPException(status_code=400, detail="unsupported chart resolution")

    lp = state.launchpad_tokens.get(token_addr)
    if lp is None:
        raise HTTPException(status_code=404, detail="launchpad token not found")

    trades: List[models.LaunchpadTrade] = state.launchpad_trades.get(token_addr, [])
    trades_sorted = sorted(trades, key=lambda t: int(t.timestamp))

    if trades_sorted:
        last_timestamp = int(trades_sorted[-1].timestamp)
    else:
        last_timestamp = int(lp.created_at or 0) or int(time.time())

    buyer_addrs: set[str] = set()
    seller_addrs: set[str] = set()
    for tr in trades_sorted:
        if tr.is_buy:
            buyer_addrs.add(tr.user)
        else:
            seller_addrs.add(tr.user)

    distinct_buyers = len(buyer_addrs)
    distinct_sellers = len(seller_addrs)

    total_holders, dev_holding, _top10 = _holders_for_token(lp.token)

    decimals = 18

    dev_addr = (lp.creator or "").lower() if getattr(lp, "creator", None) else ""
    dev_tokens_created = 0
    dev_tokens_graduated = 0
    if dev_addr:
        dev_user = state.launchpad_users.get(dev_addr)
        if dev_user is not None:
            dev_tokens_created = getattr(dev_user, "tokens_created", 0)
            dev_tokens_graduated = getattr(dev_user, "tokens_graduated", 0)

    last_price_native = getattr(lp, "last_price_native", Decimal(0))
    last_price_wad = (last_price_native) * Decimal(1e9)

    marketcap_native_raw: Decimal = last_price_native * Decimal(1e9)
    mon_price = state.tokenToPrice.get(
        "0x760afe86e5de5fa0ee542fc7b7b713e1c5425701", Decimal(0)
    )
    marketcap_usd: Decimal = marketcap_native_raw * mon_price if mon_price > 0 else Decimal(0)

    volume_native = getattr(lp, "native_volume", 0)
    volume_token = getattr(lp, "token_volume", 0)
    volume_usd = getattr(lp, "volume_usd", Decimal(0))

    mini_klines = _build_ohlcv(trades_sorted, bucket_seconds=3600, max_buckets=24)
    series_klines = _build_ohlcv(trades_sorted, bucket_seconds=chartres, max_buckets=None)

    positions_for_token = [
        pos
        for (uaddr, tkn), pos in state.launchpad_positions.items()
        if tkn == token_addr and pos.balance_token > 0
        and uaddr.lower() != "0xad720f94689edb929d9be7613223320a0b2f260f"
    ]
    positions_for_token.sort(key=lambda p: p.balance_token, reverse=True)

    holders_list: List[Dict[str, Any]] = []
    for pos in positions_for_token:
        balance_token = int(pos.balance_token)
        native_spent = int(pos.native_spent)
        native_received = int(pos.native_received)
        realized_pnl = getattr(pos, "realized_pnl_native", Decimal(0))

        current_value_native = Decimal(balance_token) * last_price_native
        unrealized_pnl_native = current_value_native
        total_pnl_native = realized_pnl + unrealized_pnl_native

        if mon_price > 0:
            balance_usd = current_value_native * mon_price
            total_pnl_usd = total_pnl_native * mon_price
        else:
            balance_usd = Decimal(0)
            total_pnl_usd = Decimal(0)

        holders_list.append(
            {
                "account": {"id": pos.user},
                "token": token_addr,
                "symbol": lp.symbol,
                "name": lp.name,
                "metadata_cid": getattr(lp, "metadata_cid", ""),
                "balance_token": str(balance_token),
                "balance_native": str(current_value_native),
                "balance_usd": str(balance_usd),
                "native_spent": str(native_spent),
                "native_received": str(native_received),
                "realized_pnl_native": str(realized_pnl),
                "unrealized_pnl_native": str(unrealized_pnl_native),
                "total_pnl_native": str(total_pnl_native),
                "total_pnl_usd": str(total_pnl_usd),
                "trade_count": int(getattr(pos, "trade_count", 0)),
                "buy_count": int(getattr(pos, "buy_count", 0)),
                "sell_count": int(getattr(pos, "sell_count", 0)),
                "tokens": str(int(pos.balance_token)),
                "tokenBought": str(int(getattr(pos, "token_bought", 0))),
                "tokenSold": str(int(getattr(pos, "token_sold", 0))),
                "nativeSpent": str(native_spent),
                "nativeReceived": str(native_received),
            }
        )

        top_traders_list: List[Dict[str, Any]] = []
    for pos in state.launchpad_positions.values():
        if getattr(pos, "token", token_addr) != token_addr:
            continue

        balance_token = int(pos.balance_token)
        native_spent = int(pos.native_spent)
        native_received = int(pos.native_received)
        realized_pnl = getattr(pos, "realized_pnl_native", Decimal(0))

        current_value_native = Decimal(balance_token) * last_price_native
        unrealized_pnl_native = current_value_native
        total_pnl_native = realized_pnl + unrealized_pnl_native

        if mon_price > 0:
            balance_usd = current_value_native * mon_price
            total_pnl_usd = total_pnl_native * mon_price
        else:
            balance_usd = Decimal(0)
            total_pnl_usd = Decimal(0)

        top_traders_list.append(
            {
                "account": {"id": pos.user},
                "token": token_addr,
                "symbol": lp.symbol,
                "name": lp.name,
                "metadata_cid": getattr(lp, "metadata_cid", ""),
                "balance_token": str(balance_token),
                "balance_native": str(current_value_native),
                "balance_usd": str(balance_usd),
                "native_spent": str(native_spent),
                "native_received": str(native_received),
                "realized_pnl_native": str(realized_pnl),
                "unrealized_pnl_native": str(unrealized_pnl_native),
                "total_pnl_native": str(total_pnl_native),
                "total_pnl_usd": str(total_pnl_usd),
                "trade_count": int(getattr(pos, "trade_count", 0)),
                "buy_count": int(getattr(pos, "buy_count", 0)),
                "sell_count": int(getattr(pos, "sell_count", 0)),
                "tokens": str(int(pos.balance_token)),
                "tokenBought": str(int(getattr(pos, "token_bought", 0))),
                "tokenSold": str(int(getattr(pos, "token_sold", 0))),
                "nativeSpent": str(native_spent),
                "nativeReceived": str(native_received),
            }
        )

    top_traders_list.sort(
        key=lambda h: Decimal(h["total_pnl_native"])
        if h.get("total_pnl_native") is not None
        else Decimal(0),
        reverse=True,
    )
    top_traders_list = top_traders_list[:50]

    recent_trades_raw = trades_sorted[-50:] if trades_sorted else []
    recent_trades_raw = list(reversed(recent_trades_raw))

    trades_out: List[Dict[str, Any]] = []
    for idx, tr in enumerate(recent_trades_raw):
        if tr.is_buy:
            amount_in = int(tr.native_amount)
            amount_out = int(tr.token_amount)
        else:
            amount_in = int(tr.token_amount)
            amount_out = int(tr.native_amount)

        trades_out.append(
            {
                "trade": {
                    "account": {"id": tr.user},
                    "amountIn": str(amount_in),
                    "amountOut": str(amount_out),
                    "block": str(int(tr.timestamp)),
                    "id": tr.txhash,
                    "isBuy": bool(tr.is_buy),
                    "priceNativePerTokenWad": str(tr.price_native),
                }
            }
        )

    tracked_addrs: set[str] = set()
    if tracked:
        for part in tracked.split(","):
            a = part.strip().lower()
            if a:
                tracked_addrs.add(a)

    tracked_trades_out: List[Dict[str, Any]] = []
    if tracked_addrs and recent_trades_raw:
        for idx, tr in enumerate(recent_trades_raw):
            if tr.user.lower() not in tracked_addrs:
                continue

            if tr.is_buy:
                amount_in = int(tr.native_amount)
                amount_out = int(tr.token_amount)
            else:
                amount_in = int(tr.token_amount)
                amount_out = int(tr.native_amount)

            tracked_trades_out.append(
                {
                    "trade": {
                        "account": {"id": tr.user},
                        "amountIn": str(amount_in),
                        "amountOut": str(amount_out),
                        "block": str(int(tr.block_number)),
                        "id": f"{lp.token}-{int(tr.block_number)}-{int(tr.timestamp)}-tracked-{idx}",
                        "isBuy": bool(tr.is_buy),
                        "priceNativePerTokenWad": str(tr.price_native),
                    }
                }
            )
            if len(tracked_trades_out) >= 50:
                break

    description = getattr(lp, "description", "") or ""
    metadata_cid = getattr(lp, "metadata_cid", "") or ""
    social1 = getattr(lp, "social1", None)
    social2 = getattr(lp, "social2", None)
    social3 = getattr(lp, "social3", None)
    social4 = getattr(lp, "social4", None)

    migrated = bool(getattr(lp, "migrated", False))
    migrated_at = getattr(lp, "migrated_at", None)
    migrated_market = getattr(lp, "market", None)

    volume_native_str = str(int(volume_native))
    volume_token_str = str(int(volume_token))
    volume_usd_str = str(volume_usd)

    dev_tokens_list: List[Dict[str, Any]] = []
    if dev_addr:
        now_ts = int(time.time())
        cutoff_ts = now_ts - 3600

        for other_token_addr, dev_lp in state.launchpad_tokens.items():
            creator_addr = (getattr(dev_lp, "creator", "") or "").lower()
            if creator_addr != dev_addr:
                continue

            dev_last_price_native = getattr(dev_lp, "last_price_native", Decimal(0))
            dev_price_wad = dev_last_price_native * Decimal(1e9)
            dev_marketcap_native = dev_last_price_native * Decimal(1e9)

            trades_for_dev = state.launchpad_trades.get(other_token_addr, [])
            vol_1h_native = 0
            for tr in trades_for_dev:
                if int(tr.timestamp) >= cutoff_ts:
                    vol_1h_native += int(tr.native_amount)

            dev_total_holders, _, _ = _holders_for_token(other_token_addr)
            dev_tokens_list.append(
                {
                    "id": dev_lp.token,
                    "name": dev_lp.name,
                    "symbol": dev_lp.symbol,
                    "metadataCID": getattr(dev_lp, "metadata_cid", ""),
                    "lastPriceNativePerTokenWad": str(dev_price_wad),
                    "marketcap": dev_marketcap_native,
                    "migrated": bool(getattr(dev_lp, "migrated", False)),
                    "volumeNative1h": str(vol_1h_native),
                    "holders": int(dev_total_holders),
                    "timestamp": str(int(dev_lp.created_at or 0)),
                }
            )

    return {
        "buyTxs": int(getattr(lp, "buy_count", 0)),
        "creator": {
            "id": dev_addr,
            "tokensGraduated": int(dev_tokens_graduated),
            "tokensLaunched": int(dev_tokens_created),
        },
        "decimals": int(decimals),
        "description": description,
        "devHoldingAmount": str(int(dev_holding)),
        "distinctBuyers": distinct_buyers,
        "distinctSellers": distinct_sellers,
        "holders": holders_list,
        "topTraders": top_traders_list,
        "devTokens": dev_tokens_list,
        "id": lp.token,
        "initialSupply": str(10**18),
        "lastPriceNativePerTokenWad": str(last_price_wad),
        "lastUpdatedAt": str(last_timestamp),
        "marketcap": marketcap_native_raw,
        "marketcap_usd": marketcap_usd,
        "metadataCID": metadata_cid,
        "migrated": migrated,
        "migratedAt": migrated_at,
        "migratedMarket": migrated_market,
        "mini": {
            "klines": mini_klines,
        },
        "name": lp.name,
        "sellTxs": int(getattr(lp, "sell_count", 0)),
        "series": {
            "klines": series_klines,
        },
        "social1": social1,
        "social2": social2,
        "social3": social3,
        "social4": social4,
        "symbol": lp.symbol,
        "timestamp": str(int(lp.created_at or 0)),
        "totalHolders": int(total_holders),
        "trackedtrades": tracked_trades_out,
        "trades": trades_out,
        "volumeNative": volume_native_str,
        "volumeToken": volume_token_str,
        "volumeUsd": volume_usd_str,
    }

@app.get("/user/{user_addr}")
def user_portfolio(user_addr: str) -> Dict[str, Any]:
    state = SEQUENCER._state
    user_addr = user_addr.lower()

    mon_price = state.tokenToPrice.get(
        "0x760afe86e5de5fa0ee542fc7b7b713e1c5425701", Decimal(0)
    )

    positions: List[Dict[str, Any]] = []

    total_value_native = Decimal(0)
    total_realized_pnl = Decimal(0)
    total_unrealized_pnl = Decimal(0)
    total_native_spent = 0
    total_native_received = 0
    total_trades = 0

    for (uaddr, tkn), pos in state.launchpad_positions.items():
        if uaddr.lower() == "0xad720f94689edb929d9be7613223320a0b2f260f" or uaddr != user_addr:
            continue

        lp = state.launchpad_tokens.get(tkn)
        if lp is None:
            continue

        last_price_native = getattr(lp, "last_price_native", Decimal(0))
        token_bought = int(getattr(pos, "token_bought", 0))
        token_sold = int(getattr(pos, "token_sold", 0))

        balance_token = int(pos.balance_token)
        native_spent = int(pos.native_spent)
        native_received = int(pos.native_received)
        realized_pnl = getattr(pos, "realized_pnl_native", Decimal(0))

        current_value_native = Decimal(balance_token) * last_price_native
        unrealized_pnl_native = current_value_native
        total_pnl_native = realized_pnl + unrealized_pnl_native
        total_value_native += current_value_native
        total_realized_pnl += realized_pnl
        total_unrealized_pnl += unrealized_pnl_native
        total_native_spent += native_spent
        total_native_received += native_received
        total_trades += int(getattr(pos, "trade_count", 0))

        if mon_price > 0:
            current_value_usd = current_value_native * mon_price
            total_pnl_usd = total_pnl_native * mon_price
        else:
            current_value_usd = Decimal(0)
            total_pnl_usd = Decimal(0)

        positions.append(
            {
                "token": tkn,
                "symbol": lp.symbol,
                "name": lp.name,
                "metadata_cid": getattr(lp, "metadata_cid", ""),
                "balance_token": str(balance_token),
                "balance_native": str(current_value_native),
                "balance_usd": str(current_value_usd),
                "native_spent": str(native_spent),
                "native_received": str(native_received),
                "realized_pnl_native": str(realized_pnl),
                "unrealized_pnl_native": str(unrealized_pnl_native),
                "total_pnl_native": str(total_pnl_native),
                "total_pnl_usd": str(total_pnl_usd),
                "trade_count": int(getattr(pos, "trade_count", 0)),
                "buy_count": int(getattr(pos, "buy_count", 0)),
                "sell_count": int(getattr(pos, "sell_count", 0)),
                "token_bought": str(token_bought),
                "token_sold": str(token_sold),
            }
        )

    positions.sort(
        key=lambda p: Decimal(p["total_pnl_native"]) if p["total_pnl_native"] is not None else Decimal(0),
        reverse=True,
    )

    if mon_price > 0:
        total_value_usd = total_value_native * mon_price
        total_pnl_native = total_realized_pnl + total_unrealized_pnl
        total_pnl_usd = total_pnl_native * mon_price
    else:
        total_value_usd = Decimal(0)
        total_pnl_native = total_realized_pnl + total_unrealized_pnl
        total_pnl_usd = Decimal(0)

    summary = {
        "user": user_addr,
        "portfolio_value_native": str(total_value_native),
        "portfolio_value_usd": str(total_value_usd),
        "realized_pnl_native": str(total_realized_pnl),
        "unrealized_pnl_native": str(total_unrealized_pnl),
        "total_pnl_native": str(total_pnl_native),
        "total_pnl_usd": str(total_pnl_usd),
        "native_spent": str(total_native_spent),
        "native_received": str(total_native_received),
        "trade_count": int(total_trades),
        "tokens_traded": len(positions),
    }

    return {
        "user": user_addr,
        "summary": summary,
        "positions": positions,
    }

@app.get("/stats/{token_addr}")
def token_stats(token_addr: str) -> Dict[str, Any]:
    state = SEQUENCER._state
    token_addr = token_addr.lower()

    lp = state.launchpad_tokens.get(token_addr)
    if lp is None:
        raise HTTPException(status_code=404, detail="launchpad token not found")

    trades = state.launchpad_trades.get(token_addr, [])

    windows: Dict[str, int] = {
        "5m": 5 * 60,
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
    }

    buckets: Dict[str, Dict[str, Any]] = {}
    for label in windows.keys():
        buckets[label] = {
            "volume_usd": Decimal(0),
            "buy_volume_usd": Decimal(0),
            "sell_volume_usd": Decimal(0),
            "buy_tx_count": 0,
            "sell_tx_count": 0,
            "prev_price_native": None,
            "start_price_native": None,
            "last_price_native": None,
        }

    out: Dict[str, Any] = {
        "type": "stats",
        "token": token_addr,
    }

    if not trades:
        for label in windows.keys():
            suffix = label
            out[f"volume_usd_{suffix}"] = 0.0
            out[f"buy_volume_usd_{suffix}"] = 0.0
            out[f"sell_volume_usd_{suffix}"] = 0.0
            out[f"buy_tx_count_{suffix}"] = 0
            out[f"sell_tx_count_{suffix}"] = 0
            out[f"change_pct_{suffix}"] = 0.0
        return out

    now_ts = int(time.time())

    trades_sorted = sorted(trades, key=lambda t: int(t.timestamp))

    for tr in trades_sorted:
        ts = int(tr.timestamp)
        price_native = Decimal(str(getattr(tr, "price_native", 0)))
        usd_amount = Decimal(str(getattr(tr, "usd_amount", 0)))

        for label, secs in windows.items():
            start_ts = now_ts - secs

            if ts <= start_ts:
                buckets[label]["prev_price_native"] = price_native
                continue

            if ts > now_ts:
                continue

            b = buckets[label]
            b["volume_usd"] += usd_amount

            if tr.is_buy:
                b["buy_volume_usd"] += usd_amount
                b["buy_tx_count"] += 1
            else:
                b["sell_volume_usd"] += usd_amount
                b["sell_tx_count"] += 1

            if b["start_price_native"] is None:
                b["start_price_native"] = price_native
            b["last_price_native"] = price_native

    INITIAL_NATIVE_PRICE = Decimal("0.00008387696")

    for label, b in buckets.items():
        suffix = label

        volume_usd = b["volume_usd"]
        buy_volume_usd = b["buy_volume_usd"]
        sell_volume_usd = b["sell_volume_usd"]
        buy_tx_count = b["buy_tx_count"]
        sell_tx_count = b["sell_tx_count"]

        prev_price = b["prev_price_native"]
        start_price = b["start_price_native"]
        last_price = b["last_price_native"]

        last_eff: Decimal | None
        if last_price is not None:
            last_eff = last_price
        else:
            last_eff = prev_price

        if prev_price is not None:
            base_price = prev_price
        elif start_price is not None:
            base_price = start_price
        else:
            base_price = INITIAL_NATIVE_PRICE

        if last_eff is None or base_price == 0:
            change_pct = 0.0
        else:
            change_pct = float((last_eff - base_price) / base_price * Decimal(100))

        out[f"volume_usd_{suffix}"] = float(volume_usd)
        out[f"buy_volume_usd_{suffix}"] = float(buy_volume_usd)
        out[f"sell_volume_usd_{suffix}"] = float(sell_volume_usd)
        out[f"buy_tx_count_{suffix}"] = int(buy_tx_count)
        out[f"sell_tx_count_{suffix}"] = int(sell_tx_count)
        out[f"change_pct_{suffix}"] = change_pct

    return out