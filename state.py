from __future__ import annotations
from typing import Dict
from decimal import Decimal, getcontext
import models
import core.storage as storage
import threading
from core import chain as h

import psycopg2

getcontext().prec = 100

INTERVALS = (1, 5, 15, 60, 300, 900, 3600, 14400, 86400)
LABEL = {300: "5m", 3600: "1h", 21600: "6h", 86400: "24h"}

RPC_HTTP = "https://rpc.monad.xyz"

class State:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        
        # launchpad
        self.launchpad_tokens: Dict[str, models.LaunchpadToken] = {} # tokenAddress -> LaunchpadToken
        self.launchpad_market_to_token: Dict[str, str] = {} # market/pool -> tokenAddress

        # graduated launchpad
        self.v3_pools: Dict[str, models.PoolInfo] = {} # poolAddress -> PoolInfo
        self.token_to_v3_pool: Dict[str, str] = {} # tokenAddress -> poolAddress
        
        self.mon_price_usd = Decimal("0.03")
        
    # oracle
    def set_mon_price_usd(self, value) -> None:
        try:
            px = Decimal(value)
        except Exception:
            return

        if px <= 0:
            return

        self.mon_price_usd = px

        try:
            storage.set_mon_price_usd(px)
        except Exception as e:
            print(f"[State] failed to persist mon_price_usd: {e!r}")
        
    # reconstruction
    def rebuild_from_db(self) -> None:
        with self._lock:
            self.launchpad_tokens.clear()
            self.launchpad_market_to_token.clear()
            self.v3_pools.clear()
            self.token_to_v3_pool.clear()

            token_rows = storage.load_tokens_for_state()
            for row in token_rows:
                (
                    token,
                    creator,
                    name,
                    symbol,
                    metadata_cid,
                    description,
                    social1,
                    social2,
                    social3,
                    social4,
                    source,
                    created_block,
                    created_at,
                    migrated,
                    migrated_block,
                    migrated_at,
                    market,
                    last_price_native,
                    native_volume,
                    token_volume,
                    volume_usd,
                    fees_usd,
                    buy_count,
                    sell_count,
                    tx_count,
                    circulating_supply,
                    snipers_count,
                    approaching_75,
                    approaching_75_block,
                    approaching_75_at,
                ) = row

                lp = models.LaunchpadToken(
                    token=token,
                    creator=creator,
                    name=name,
                    symbol=symbol,
                    metadata_cid=metadata_cid,
                    description=description,
                    social1=social1,
                    social2=social2,
                    social3=social3,
                    social4=social4,
                    source=int(source),
                    created_block=int(created_block),
                    created_at=int(created_at),
                    migrated=bool(migrated),
                    migrated_block=int(migrated_block) if migrated_block is not None else None,
                    migrated_at=int(migrated_at) if migrated_at is not None else None,
                    market=market,
                    last_price_native=Decimal(last_price_native),
                    native_volume=int(native_volume),
                    token_volume=int(token_volume),
                    volume_usd=Decimal(volume_usd),
                    fees_usd=Decimal(fees_usd),
                    buy_count=int(buy_count),
                    sell_count=int(sell_count),
                    tx_count=int(tx_count),
                    circulating_supply=int(circulating_supply),
                    snipers=int(snipers_count),
                    approaching_75=bool(approaching_75),
                    approaching_75_block=int(approaching_75_block) if approaching_75_block is not None else 0,
                    approaching_75_at=int(approaching_75_at) if approaching_75_at is not None else 0,
                )

                self.launchpad_tokens[token.lower()] = lp
                if market:
                    self.launchpad_market_to_token[market.lower()] = token.lower()

            pool_rows = storage.load_all_pools()
            for pool, token_addr, native_addr, token_is_0 in pool_rows:
                pi = models.PoolInfo(
                    pool=pool.lower(),
                    token_addr=token_addr.lower(),
                    native_addr=native_addr.lower(),
                    token_is_0=bool(token_is_0),
                )
                self.v3_pools[pi.pool] = pi
                self.token_to_v3_pool[pi.token_addr] = pi.pool
                
                if pool.lower() not in h.ADDRS:
                    h.ADDRS.append(pool.lower())
                    
            try:
                stored = storage.get_mon_price_usd()
                if stored is not None and stored > 0:
                    self.mon_price_usd = Decimal(stored)
            except Exception as e:
                print(f"[State] Failed to load MON price from DB: {e!r}")

    def reset_for_reindex(self) -> None:
        with self._lock:
            self.launchpad_tokens.clear()
            self.launchpad_market_to_token.clear()
            self.v3_pools.clear()
            self.token_to_v3_pool.clear()

            self.rebuild_from_db()
            print(f"[State] Reset for reindex: {len(self.launchpad_tokens)} tokens, {len(self.v3_pools)} pools")

    # launchpad

    # apply token creation
    def apply_token_created(self, blk: int, ev: dict, ts: int, log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> None:
        with self._lock:
            if log_addr.lower() != h.CONTRACTS["NADFUN"].lower():
                return
            
            token = ev.get("token","").lower()
            if not token:
                return
            
            creator = ev.get("creator", "").lower()
            name = ev.get("name", "")
            symbol = ev.get("symbol", "")
            metadata_cid = ev.get("metadata_cid", "")
            description = ev.get("description", "")
            social1 = ev.get("social1", "")
            social2 = ev.get("social2", "")
            social3 = ev.get("social3", "")
            social4 = ev.get("social4", "")
            source = int(ev.get("source", 0))
            
            lp = self.launchpad_tokens.get(token)
            if lp is not None:
                return
            else:
                lp = models.LaunchpadToken(
                    token=token,
                    creator=creator,
                    name=name,
                    symbol=symbol,
                    metadata_cid=metadata_cid,
                    description=description,
                    social1=social1,
                    social2=social2,
                    social3=social3,
                    social4=social4,
                )
                lp.created_block = blk
                lp.created_at = ts
                lp.source = source
                self.launchpad_tokens[token] = lp
                
                if (source == 1):
                    lp.last_price_native = ev.get(
                        "last_price_native", 
                        Decimal("0.00008387696")
                    )
                
            storage.upsert_token_created(
                token=token,
                creator=creator,
                name=name,
                symbol=symbol,
                metadata_cid=metadata_cid,
                description=description,
                social1=social1,
                social2=social2,
                social3=social3,
                social4=social4,
                source=source,
                created_block=blk,
                created_at=ts,
                last_price_native=lp.last_price_native,
                cur=cur
            )
            
            if creator:                
                storage.increment_user_tokens_created(creator, cur=cur)

    # applies a trade
    def apply_launchpad_trade(self, ev: dict, blk: int, ts: int, txh: str, log_idx: int, _log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> None:
        with self._lock:
            is_v3_swap = "pool" in ev and "amount0" in ev and "amount1" in ev
            if not is_v3_swap:
                token = ev.get("token", "").lower()
                user = ev.get("user", "").lower()
                if not token or not user:
                    return
                
                is_buy = bool(ev.get("is_buy", False))
                amount_in = int(ev.get("amount_in", 0) or 0)
                amount_out = int(ev.get("amount_out", 0) or 0)
                if amount_in <= 0 and amount_out <= 0:
                    return
                
                native_amt = amount_in if is_buy else amount_out
                token_amt = amount_out if is_buy else amount_in
                
                if native_amt <= 0 or token_amt <= 0:
                    return
                
                try:
                    price_native = Decimal(ev.get("native_reserve")) / Decimal(ev.get("token_reserve"))
                except Exception:
                    try:
                        price_native = Decimal(native_amt) / Decimal(token_amt)
                    except Exception:
                        price_native = Decimal(0)
            else:
                pool_addr = (ev.get("pool") or "").lower()
                pi = self.v3_pools.get(pool_addr)
                if pi is None:
                    return

                token = (pi.token_addr or "").lower()
                user = (ev.get("user") or "").lower()

                if not token or not user:
                    return

                try:
                    amount0 = int(ev.get("amount0") or 0)
                    amount1 = int(ev.get("amount1") or 0)
                except Exception:
                    return

                if amount0 == 0 and amount1 == 0:
                    return

                if pi.token_is_0:
                    token_delta = amount0
                    native_delta = amount1
                else:
                    token_delta = amount1
                    native_delta = amount0

                if native_delta == 0 or token_delta == 0:
                    return

                is_buy = native_delta > 0

                if is_buy:
                    native_amt = native_delta
                    token_amt = -token_delta
                else:
                    native_amt = -native_delta
                    token_amt = token_delta

                if native_amt <= 0 or token_amt <= 0:
                    return

                is_buy_flag = is_buy
                amount_in = native_amt if is_buy_flag else token_amt
                amount_out = token_amt if is_buy_flag else native_amt

                price_raw = ev.get("sqrt_price_x96") or 0
                try:
                    sqrt_p = Decimal(int(price_raw)) / Decimal(1 << 96)
                    ratio = sqrt_p * sqrt_p

                    if ratio <= 0:
                        price_native = Decimal(0)
                    else:
                        if pi.token_is_0:
                            price_native = ratio
                        else:
                            price_native = Decimal(1) / ratio
                except Exception:
                    price_native = Decimal(0)

            lp = self.launchpad_tokens.get(token)
            if lp is None:
                return
            
            lp.last_price_native = price_native
                
            if not is_v3_swap:
                if is_buy and blk <= lp.created_block + 10:
                    creator_addr = (lp.creator or "").lower()
                    user_addr = user.lower()
                    if user_addr and user_addr != creator_addr:
                        if batch is not None:
                            batch.add_sniper(token, user_addr)
                            lp.snipers += 1  # approximate count for batch mode
                        else:
                            inserted = storage.add_sniper_address(token, user_addr, cur=cur)
                            if inserted:
                                lp.snipers += 1

                if is_buy:
                    lp.circulating_supply += token_amt / 1e18
                else:
                    lp.circulating_supply -= token_amt / 1e18
            
                if lp.source == 0:
                    if (not lp.approaching_75) and ev.get("native_reserve") >= 2_500_000_000_000_000_000_000:
                        lp.approaching_75 = True
                        lp.approaching_75_block = blk
                        lp.approaching_75_at = ts
                    elif (lp.approaching_75) and ev.get("native_reserve") < 2_500_000_000_000_000_000_000:
                        lp.approaching_75 = False
                        lp.approaching_75_block = 0
                        lp.approaching_75_at = 0
                elif lp.source == 1:
                    if (not lp.approaching_75) and lp.circulating_supply >= 594_825_000:
                        lp.approaching_75 = True
                        lp.approaching_75_block = blk
                        lp.approaching_75_at = ts
                    elif (lp.approaching_75) and lp.circulating_supply < 594_825_000:
                        lp.approaching_75 = False
                        lp.approaching_75_block = 0
                        lp.approaching_75_at = 0
            
            lp.native_volume += native_amt
            lp.token_volume += token_amt
            lp.tx_count += 1
            if is_buy:
                lp.buy_count += 1
            else:
                lp.sell_count += 1

            mon_price = self.mon_price_usd
            if mon_price > 0:
                volume_usd_trade = (Decimal(native_amt) / (Decimal(10) ** 18)) * mon_price
                lp.volume_usd += volume_usd_trade
                lp.fees_usd += volume_usd_trade * Decimal("0.01")

            if is_buy:
                token_bought_delta = int(token_amt)
                token_sold_delta = 0
                native_spent_delta = int(native_amt)
                native_received_delta = 0
                balance_token_delta = 0
                realized_delta = Decimal(-native_amt)
                buy_count_delta = 1
                sell_count_delta = 0
            else:
                token_bought_delta = 0
                token_sold_delta = int(token_amt)
                native_spent_delta = 0
                native_received_delta = int(native_amt)
                balance_token_delta = 0
                realized_delta = Decimal(native_amt)
                buy_count_delta = 0
                sell_count_delta = 1

            trade_count_delta = 1
            
            usd_amount = Decimal(native_amt) * Decimal(0.05)

            if batch is not None:
                # Batch mode: accumulate writes
                batch.add_trade(
                    block_number=blk,
                    log_index=log_idx,
                    timestamp=ts,
                    token=token,
                    user_address=user,
                    is_buy=is_buy,
                    native_amount=int(native_amt),
                    token_amount=int(token_amt),
                    usd_amount=usd_amount,
                    price_native=lp.last_price_native,
                    txhash=txh,
                )
                batch.set_token_state(token, {
                    "last_price_native": lp.last_price_native,
                    "native_volume": int(lp.native_volume),
                    "token_volume": int(lp.token_volume),
                    "volume_usd": lp.volume_usd,
                    "fees_usd": lp.fees_usd,
                    "buy_count": lp.buy_count,
                    "sell_count": lp.sell_count,
                    "tx_count": lp.tx_count,
                    "circulating_supply": lp.circulating_supply,
                    "approaching_75": lp.approaching_75,
                    "approaching_75_block": lp.approaching_75_block,
                    "approaching_75_at": lp.approaching_75_at,
                    "snipers_count": lp.snipers,
                })
                batch.add_user_delta(user, int(native_amt), realized_delta)
                batch.add_position_delta(
                    user_address=user,
                    token=token,
                    token_bought_delta=token_bought_delta,
                    token_sold_delta=token_sold_delta,
                    native_spent_delta=native_spent_delta,
                    native_received_delta=native_received_delta,
                    balance_token_delta=balance_token_delta,
                    realized_pnl_delta=realized_delta,
                    trade_count_delta=trade_count_delta,
                    buy_count_delta=buy_count_delta,
                    sell_count_delta=sell_count_delta,
                    last_price_native=lp.last_price_native,
                )
                for bucket_seconds in INTERVALS:
                    bucket_start = (int(ts) // bucket_seconds) * bucket_seconds
                    batch.add_ohlcv(token, bucket_seconds, bucket_start, lp.last_price_native, int(native_amt))
            else:
                # Direct mode: execute writes immediately
                storage.insert_trade(
                    block_number=blk,
                    log_index=log_idx,
                    timestamp=ts,
                    token=token,
                    user_address=user,
                    is_buy=is_buy,
                    native_amount=int(native_amt),
                    token_amount=int(token_amt),
                    usd_amount=usd_amount,
                    price_native=lp.last_price_native,
                    txhash=txh,
                    cur=cur,
                )
                storage.update_token_after_trade(
                    token=token,
                    last_price_native=lp.last_price_native,
                    native_volume=int(lp.native_volume),
                    token_volume=int(lp.token_volume),
                    volume_usd=lp.volume_usd,
                    fees_usd=lp.fees_usd,
                    buy_count=lp.buy_count,
                    sell_count=lp.sell_count,
                    tx_count=lp.tx_count,
                    circulating_supply=lp.circulating_supply,
                    approaching_75=lp.approaching_75,
                    approaching_75_block=lp.approaching_75_block,
                    approaching_75_at=lp.approaching_75_at,
                    snipers_count=lp.snipers,
                    cur=cur,
                )
                storage.update_user_on_trade(
                    address=user,
                    native_amount=int(native_amt),
                    realized_delta=realized_delta,
                    cur=cur,
                )

                storage.upsert_position(
                    user_address=user,
                    token=token,
                    token_bought_delta=token_bought_delta,
                    token_sold_delta=token_sold_delta,
                    native_spent_delta=native_spent_delta,
                    native_received_delta=native_received_delta,
                    balance_token_delta=balance_token_delta,
                    realized_pnl_delta=realized_delta,
                    trade_count_delta=trade_count_delta,
                    buy_count_delta=buy_count_delta,
                    sell_count_delta=sell_count_delta,
                    last_price_native=lp.last_price_native,
                    cur=cur,
                )

                for bucket_seconds in INTERVALS:
                    bucket_start = (int(ts) // bucket_seconds) * bucket_seconds
                    storage.upsert_ohlcv(
                        token=token,
                        resolution_sec=bucket_seconds,
                        bucket_start=bucket_start,
                        price_native=lp.last_price_native,
                        native_amount=int(native_amt),
                        cur=cur,
                    )

    # applies graduation/migration
    def apply_migrated(self, blk: int, ts: int, ev: dict, log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> str | None:
        with self._lock:
            if log_addr.lower() != h.CONTRACTS["NADFUN"].lower():
                return None
            
            token = (ev.get("token") or "").lower()
            if not token:
                return None

            lp = self.launchpad_tokens.get(token)
            if lp is None:
                return None

            lp.migrated = True
            lp.migrated_block = blk
            lp.migrated_at = ts

            pool = (ev.get("pool") or "").lower()
            
            if pool and pool not in self.v3_pools:
                wmon = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A".lower()

                lp.market = pool

                if token != wmon:
                    token_is_0 = token < wmon

                    self.v3_pools[pool] = models.PoolInfo(
                        pool=pool,
                        token_addr=token,
                        native_addr=wmon,
                        token_is_0=token_is_0,
                    )
                    self.token_to_v3_pool[token] = pool
                    
                    storage.upsert_pool(
                        pool=pool,
                        token_addr=token,
                        native_addr=wmon,
                        token_is_0=token_is_0,
                        cur=cur,
                    )
                    
            storage.mark_token_migrated(
                token=token,
                migrated_block=blk,
                migrated_at=ts,
                pool=pool or None,
                cur=cur,
            )
            
            if pool:
                storage.clear_position(user_address=pool, token=token, cur=cur)

            creator = lp.creator.lower() if lp.creator else ""
            if creator:               
                storage.increment_user_tokens_graduated(creator, cur=cur)

        return pool or None

    # keeps track of txfers for balances
    def apply_token_transfer(self, ev: dict, blk: int, ts: int, _log_addr: str, cur: psycopg2.extensions.cursor | None = None, batch=None) -> None:
        with self._lock:
            token = (ev.get("token") or "").lower()
            if not token:
                return

            lp = self.launchpad_tokens.get(token)
            if lp is None:
                return

            amount = int(ev.get("amount", 0) or 0)
            if amount <= 0:
                return

            from_addr = (ev.get("from") or "").lower()
            to_addr = (ev.get("to") or "").lower()

            zero = "0x" + "0" * 40
            internal = {a.lower() for a in getattr(h, "ADDRS", [])}

            def adjust(user: str, delta: int) -> None:
                addr = (user or "").lower()
                if not addr or addr == zero or delta == 0:
                    return
                if addr in internal:
                    return

                if batch is not None:
                    batch.add_position_delta(
                        user_address=addr,
                        token=token,
                        token_bought_delta=0,
                        token_sold_delta=0,
                        native_spent_delta=0,
                        native_received_delta=0,
                        balance_token_delta=int(delta),
                        realized_pnl_delta=Decimal(0),
                        trade_count_delta=0,
                        buy_count_delta=0,
                        sell_count_delta=0,
                        last_price_native=lp.last_price_native,
                    )
                else:
                    storage.upsert_position(
                        user_address=addr,
                        token=token,
                        token_bought_delta=0,
                        token_sold_delta=0,
                        native_spent_delta=0,
                        native_received_delta=0,
                        balance_token_delta=int(delta),
                        realized_pnl_delta=Decimal(0),
                        trade_count_delta=0,
                        buy_count_delta=0,
                        sell_count_delta=0,
                        last_price_native=lp.last_price_native,
                        cur=cur,
                    )

            adjust(from_addr, -amount)
            adjust(to_addr, amount)