from __future__ import annotations
from decimal import Decimal
from typing import Dict, Any, List, Tuple
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import time
import traceback

from core.sequencer import SEQUENCER
from api.x_api import router as x_router
import models

# helpers
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
    marketcap_usd: Decimal = marketcap_native_raw * Decimal(0.05)

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
        native_amt = int(tr.native_amount)

        b = buckets.get(bucket_start)
        if b is None:
            b = {
                "time": bucket_start,
                "open": price_wad,
                "high": price_wad,
                "low": price_wad,
                "close": price_wad,
                "quoteVolume": native_amt,
            }
            buckets[bucket_start] = b
        else:
            b["close"] = price_wad
            if price_wad > b["high"]:
                b["high"] = price_wad
            if price_wad < b["low"]:
                b["low"] = price_wad
            b["quoteVolume"] += native_amt

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
                "quoteVolume": str(int(b["quoteVolume"])),
            }
        )
    return out


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

# terminal
@app.get("/tokens") # the 30 30 30 list
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

    recent_created_out: List[Dict[str, Any]] = []
    for t in recent_created:
        row = _serialize_token(t.token)
        row["graduationPercentageBps"] = t.circulating_supply / 793100000
        recent_created_out.append(row)

    recent_approaching_out: List[Dict[str, Any]] = []
    for t in recent_approaching:
        row = _serialize_token(t.token)
        row["graduationPercentageBps"] = t.circulating_supply / 793100000
        recent_approaching_out.append(row)

    recent_graduated_out: List[Dict[str, Any]] = []
    for t in recent_graduated:
        row = _serialize_token(t.token)
        row["graduationPercentageBps"] = t.circulating_supply / 793100000
        recent_graduated_out.append(row)

    return {
        "recent_created": recent_created_out,
        "recent_approaching": recent_approaching_out,
        "recent_graduated": recent_graduated_out,
    }


@app.get("/token/{token_addr}/{chartres}") # memeinterface
def token_overview_graph(
    token_addr: str,
    chartres: int,
    tracked: str = Query(
        "",
        description="comma-separated list of addresses to track for trackedtrades",
    ),
) -> Dict[str, Any]:
    try:
        state = SEQUENCER._state
        token_addr = token_addr.lower()

        if chartres not in (1, 5, 15, 60, 300, 900, 3600, 14400, 86400):
            raise HTTPException(status_code=400)

        lp = state.launchpad_tokens.get(token_addr)
        if lp is None:
            raise HTTPException(status_code=404)

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
        mon_price = Decimal(0.05)
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

            if getattr(pos, "user", "").lower() == "0xad720f94689edb929d9be7613223320a0b2f260f":
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
                    "metadata_cid": getattr(pos, "metadata_cid", ""),
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
        
        graduation_bps = getattr(lp, "circulating_supply", 0) / 793100000

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
            "graduationPercentageBps": graduation_bps,
        }
    except Exception:
        print(f"[token_overview_graph] error token={token_addr}")
        traceback.print_exc()
        raise
    
    
@app.get("/user/{user_addr}") # positions (later pnl)
def user_portfolio(user_addr: str) -> Dict[str, Any]:
    state = SEQUENCER._state
    user_addr = user_addr.lower()

    mon_price = Decimal(0.05)

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


@app.get("/stats/{token_addr}") # stats thing
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
        elif prev_price is not None:
            last_eff = prev_price
        else:
            last_eff = INITIAL_NATIVE_PRICE

        if prev_price is not None:
            base_price = prev_price
        else:
            base_price = INITIAL_NATIVE_PRICE

        if base_price == 0:
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

# bz
@app.get("/volume/{user_addr}")
def user_volume(user_addr: str) -> Dict[str, Any]:
    state = SEQUENCER._state
    user_addr = user_addr.lower()

    total_native_volume = 0
    total_trades = 0

    seen_tokens: set[str] = set()

    for (uaddr, tkn), pos in state.launchpad_positions.items():
        if uaddr.lower() != user_addr:
            continue

        native_spent = int(getattr(pos, "native_spent", 0))
        native_received = int(getattr(pos, "native_received", 0))
        trade_count = int(getattr(pos, "trade_count", 0))

        total_native_volume += native_spent + native_received
        total_trades += trade_count

        if trade_count > 0 and tkn not in seen_tokens:
            seen_tokens.add(tkn)

    total_native_volume_dec = Decimal(total_native_volume)

    return {
        "user": user_addr,
        "volume_native": str(total_native_volume_dec),
        "trade_count": int(total_trades),
        "tokens_traded": len(seen_tokens),
    }

# hi
@app.get("/search/query")
def search_tokens(
    query: str = Query(
        ...,
        min_length=1,
        max_length=64,
        description="search string for token name, symbol, or address",
    ),
    limit: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    state = SEQUENCER._state
    q = query.strip().lower()
    if not q:
        raise HTTPException(status_code=400, detail="empty query")

    scored: List[tuple[int, models.LaunchpadToken]] = []

    for lt in state.launchpad_tokens.values():
        name = (lt.name or "").lower()
        symbol = (lt.symbol or "").lower()
        addr = (lt.token or "").lower()

        score = 0

        if q == symbol:
            score += 100
        if q == name:
            score += 90
        if q == addr:
            score += 80

        if symbol.startswith(q):
            score += 60
        if name.startswith(q):
            score += 50
        if addr.startswith(q):
            score += 40

        if q in symbol:
            score += 30
        if q in name:
            score += 20
        if q in addr:
            score += 10

        if score > 0:
            scored.append((score, lt))

    scored.sort(
        key=lambda item: (item[0], getattr(item[1], "created_at", 0)),
        reverse=True,
    )

    results: List[Dict[str, Any]] = []
    for score, lt in scored[:limit]:
        row = _serialize_token(lt.token)
        row["search_score"] = score
        results.append(row)

    return {
        "query": query,
        "count": len(results),
        "results": results,
    }
