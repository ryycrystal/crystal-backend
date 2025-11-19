from __future__ import annotations
from dataclasses import is_dataclass, asdict
from typing import Dict, Deque, List, Tuple, Set
from collections import deque
from decimal import Decimal, getcontext
import json
import urllib.request
import time
import models
import os
import sys

getcontext().prec = 50

INTERVALS = (300, 3600, 21600, 86400)
LABEL = {300: "5m", 3600: "1h", 21600: "6h", 86400: "24h"}

RPC_HTTP = "https://testnet-rpc.monad.xyz"

class State:
    def __init__(self) -> None:
        # markets n prices n tokens
        self.addressToMarket: Dict[str, models.MarketInfo] = {}
        self.tokenToPrice: Dict[str, Decimal] = {}
        self.tokenGraph: Dict[str, List[models.MarketInfo]] = {}
        self.tokenToPrice["0xf817257fed379853cde0fa4f97ab987181b1e5ea"] = Decimal(1)

        # vaults
        self.allVaults: Set[str] = set()
        self.vaults: Dict[str, models.Vault] = {}
        self.vaultBalancesDay: Dict[str, Deque[models.VaultBalance]] = {}
        self.vaultBalancesWeek: Dict[str, Deque[models.VaultBalance]] = {}
        self.vaultBalancesMonth: Dict[str, Deque[models.VaultBalance]] = {}
        self.vaultBalancesAllTime: Dict[str, Deque[models.VaultBalance]] = {}
        self.vaultToDeposits: Dict[str, List[models.VaultDeposit]] = {}
        self.vaultToWithdraws: Dict[str, List[models.VaultWithdraw]] = {}
        self.vaultToUsers: Dict[str, Dict[str, models.VaultUser]] = {}
        self.vaultLatest: Dict[str, Dict[str, int]] = {}

        # amm lp
        self.ammPools: Dict[str, models.AMMPool] = {}
        self.ammEvents24h: Dict[str, Deque[dict]] = {}
        self.ammVolume24h: Dict[str, float] = {}
        self.ammHistory: Dict[str, List[dict]] = {}

        # launchpad
        self.launchpad_tokens: Dict[str, models.LaunchpadToken] = {}
        self.launchpad_users: Dict[str, models.LaunchpadUser] = {}
        self.launchpad_positions: Dict[tuple[str, str], models.LaunchpadPosition] = {}
        self.launchpad_market_to_token: Dict[str, str] = {}
        self.launchpad_trades: Dict[str, List[models.LaunchpadTrade]] = {}

        if True:
            self.seed_single_market()
            self.sweep()

    # debugging
    def seed_single_market(self) -> None:
        m = {
            "id": "0xA4dCef430fc8056713e6865fe64b6150e541Ef23",
            "baseAsset": "0x760afe86e5de5fa0ee542fc7b7b713e1c5425701",
            "quoteAsset": "0xf817257fed379853cde0fa4f97ab987181b1e5ea",
            "baseDecimals": 18,
            "quoteDecimals": 6,
            "baseTicker": "WMON",
            "quoteTicker": "USDC",
            "baseName": "Wrapped Monad",
            "quoteName": "USD Coin",
            "marketType": 2,
            "scaleFactor": 21,
            "tickSize": 1,
            "minSize": 1_000_000,
            "maxPrice": 1_000_000_000_000_000,
            "takerFee": 99_950,
            "makerRebate": 99_990,
            "latestPrice": 0,
        }

        pf = int(m["quoteDecimals"]) + int(m["scaleFactor"]) - int(m["baseDecimals"])
        price_dec = Decimal(int(m["latestPrice"])) / (Decimal(10) ** pf)

        mi = models.MarketInfo(
            isCanonical=True,
            quoteAsset=m["quoteAsset"].lower(),
            baseAsset=m["baseAsset"].lower(),
            market=m["id"].lower(),
            quoteAddress=m["quoteAsset"].lower(),
            quoteDecimals=int(m["quoteDecimals"]),
            quoteTicker=m["quoteTicker"],
            quoteName=m["quoteName"],
            baseAddress=m["baseAsset"].lower(),
            baseDecimals=int(m["baseDecimals"]),
            baseTicker=m["baseTicker"],
            baseName=m["baseName"],
            marketId=0,
            marketType=int(m["marketType"]),
            scaleFactor=int(m["scaleFactor"]),
            tickSize=int(m["tickSize"]),
            maxPrice=int(m["maxPrice"]),
            minSize=int(m["minSize"]),
            takerFee=int(m["takerFee"]),
            makerRebate=int(m["makerRebate"]),
            price=price_dec,
        )

        self.apply_market_created(mi, mi.market, "")
        self.addressToMarket[mi.market] = mi

        if mi.isCanonical and int(mi.marketType) not in (0, 1):
            if not hasattr(self, "ammPools"):
                self.ammPools = {}
            maddr = mi.market.lower()
            if maddr not in self.ammPools:
                self.ammPools[maddr] = models.AMMPool(
                    market=maddr,
                    quote=mi.quoteAddress.lower(),
                    base=mi.baseAddress.lower(),
                    marketType=int(mi.marketType),
                    quoteDecimals=int(mi.quoteDecimals),
                    baseDecimals=int(mi.baseDecimals),
                    quoteTicker=mi.quoteTicker,
                    quoteName=mi.quoteName,
                    baseTicker=mi.baseTicker,
                    baseName=mi.baseName,
                    feeBps=int(getattr(mi, "takerFee", 0)),
                    reserveQuote=0,
                    reserveBase=0,
                    tvlUsd=Decimal(0),
                    volume24hUsd=Decimal(0),
                    created=time.time()
                )

    # market creation, prices
    def apply_market_created(self, ev: models.MarketInfo, ts: int, _log_addr: str) -> None:
        if not ev.isCanonical:
            return

        base = ev.baseAddress.lower()
        quote = ev.quoteAddress.lower()
        market = ev.market.lower()

        self.addressToMarket[market] = ev

        # create amm pool
        if int(ev.marketType) not in (0, 1):
            if market not in self.ammPools:
                fee_bps = 25
                self.ammPools[market] = models.AMMPool(
                    market=market,
                    quote=ev.quoteAddress.lower(),
                    base=ev.baseAddress.lower(),
                    marketType=int(ev.marketType),
                    quoteDecimals=int(ev.quoteDecimals),
                    baseDecimals=int(ev.baseDecimals),
                    quoteTicker=ev.quoteTicker,
                    baseTicker=ev.baseTicker,
                    quoteName=ev.quoteName,
                    baseName=ev.baseName,
                    feeBps=fee_bps,
                    reserveQuote=0,
                    reserveBase=0,
                    tvlUsd=Decimal(0),
                    volume24hUsd=Decimal(0),
                    created=ts,
                )
                self.ammEvents24h[market] = deque()
                self.ammVolume24h[market] = 0.0
                self.ammHistory[market] = []

        # default a market price so it dont crash
        try:
            if getattr(ev, "price", None) is None:
                ev.price = Decimal(0)
        except Exception:
            pass
       
        # attempt to link to launchpad token if this market was created from a migration
        lp = self.launchpad_tokens.get(base)
        if lp and lp.migrated:
            if not lp.market:
                lp.market = market
            self.launchpad_market_to_token[market] = base

        add_to_graph = True
        if lp and not lp.migrated:
            add_to_graph = False

        # token graph
        if add_to_graph:
            lst_base = self.tokenGraph.setdefault(base, [])
            lst_base.append(ev)

            lst_quote = self.tokenGraph.setdefault(quote, [])
            lst_quote.append(ev)

    def apply_trade(self, ev: models.Trade, blk: int, ts: int, _log_addr: str) -> None:
        market = ev.market.lower()
        mi = self.addressToMarket.get(market)
        if mi is None:
            return

        # update market price
        try:
            pf = int(mi.quoteDecimals) + int(mi.scaleFactor) - int(mi.baseDecimals)
            if pf < 0:
                return
        except Exception:
            return

        if ev.end_price and ev.end_price > 0:
            try:
                mi.price = Decimal(ev.end_price) / (Decimal(10) ** pf)
            except Exception:
                return
            
        # check for launchpad token linked to market (below here is launchpad stats)
        token_addr = self.launchpad_market_to_token.get(market)
        if not token_addr:
            return
        
        base = mi.baseAddress.lower()
        quote = mi.quoteAddress.lower()
        
        if base != token_addr or quote != "0x760afe86e5de5fa0ee542fc7b7b713e1c5425701":
            return
        
        user = ev.user.lower()
        is_buy = bool(ev.is_buy)
        amount_in = int(ev.amount_in or 0)
        amount_out = int(ev.amount_out or 0)
        if amount_in <= 0 and amount_out <= 0:
            return
        
        if is_buy:
            native_amt = amount_in
            token_amt = amount_out
            token_is_buy = True
        else:
            native_amt = amount_out
            token_amt = amount_in
            token_is_buy = False
            
        if native_amt <= 0 or token_amt <= 0:
            return
        
        # check launchpad token itself (this shouldnt hit)
        lp = self.launchpad_tokens.get(token_addr)
        if lp is None:
            print("[State] Error: Normal Trade emitted for nonexistant launchpad token")
        
        # update latest price in mon/token
        try:
            price_native = Decimal(native_amt) / Decimal(token_amt)
        except Exception:
            price_native = Decimal(0)
        lp.last_price_native = price_native

        # per-token aggregate stats
        lp.native_volume += native_amt
        lp.token_volume += token_amt
        lp.tx_count += 1
        if token_is_buy:
            lp.buy_count += 1
        else:
            lp.sell_count += 1
        
        # usd volume (all-time), no fee increment here
        mon_price = self.tokenToPrice.get("0x760afe86e5de5fa0ee542fc7b7b713e1c5425701", Decimal(0))
        if mon_price > 0:
            volume_usd_trade = (Decimal(native_amt) / (Decimal(10) ** 18)) * mon_price
            lp.volume_usd += volume_usd_trade
        
        # per-user and per-token position
        lu = self.launchpad_users.get(user)
        if lu is None:
            lu = models.LaunchpadUser(address=user)
            self.launchpad_users[user] = lu
        lu.total_trades += 1
        key = (user, token_addr)
        pos = self.launchpad_positions.get(key)
        if pos is None:
            pos = models.LaunchpadPosition(user=user, token=token_addr)
            self.launchpad_positions[key] = pos

        pos.trade_count += 1
        if token_is_buy:
            pos.buy_count += 1
            pos.token_bought += token_amt
            pos.native_spent += native_amt
            pos.balance_token += token_amt
        else:
            pos.sell_count += 1
            pos.token_sold += token_amt
            pos.native_received += native_amt
            pos.balance_token -= token_amt
            if pos.balance_token < 0:
                pos.balance_token = 0
        
        old_realized = pos.realized_pnl_native
        realized_native = pos.native_received - pos.native_spent
        pos.realized_pnl_native = Decimal(realized_native)
        
        delta = pos.realized_pnl_native - old_realized
        lu.total_realized_pnl_native += delta
        
        trades = self.launchpad_trades.setdefault(token_addr, [])
        trades.append(
            models.LaunchpadTrade(
                block_number=blk,
                timestamp=int(ts),
                token=token_addr,
                user=user,
                is_buy=token_is_buy,
                native_amount=int(native_amt),
                token_amount=int(token_amt),
                price_native=lp.last_price_native,
            )
        )
        if len(trades) > 500000:
            trades[:] = trades[-500000:]

    def sweep(self) -> None:
        root = "0xf817257fed379853cde0fa4f97ab987181b1e5ea"
        self.tokenToPrice[root] = Decimal(1)

        visited: Dict[str, bool] = {}
        q = deque([root])

        while q:
            token = q.popleft()
            if visited.get(token):
                continue
            visited[token] = True

            usd_q = self.tokenToPrice.get(token, Decimal(0))
            if usd_q <= 0:
                continue

            for m in self.tokenGraph.get(token, []):
                try:
                    qa = m.quoteAddress.lower()
                    ba = m.baseAddress.lower()
                except Exception:
                    continue

                if qa != token:
                    continue

                r = getattr(m, "price", None)
                if r is None or r <= 0:
                    continue

                new = r * usd_q
                old = self.tokenToPrice.get(ba)

                if old is None or new != old:
                    self.tokenToPrice[ba] = new
                    q.append(ba)

    def token_price(self, token: str) -> float:
        v = self.tokenToPrice.get(token.lower())
        return float(v) if v is not None else 0.0

    # strategy vaults
    def apply_vault_deployed(self, ts: int, ev: models.Vault, _log_addr: str) -> None:
        v = ev.vault.lower()
        if v in self.vaults:
            return

        quote = ev.quote.lower()
        base = ev.base.lower()
        market = ""
        qd = 0
        bd = 0

        for mi in self.tokenGraph.get(quote, []):
            if mi.quoteAddress.lower() == quote and mi.baseAddress.lower() == base:
                market = mi.market.lower()
                try:
                    qd = int(mi.quoteDecimals)
                    bd = int(mi.baseDecimals)
                except Exception:
                    pass
                break

        vault_obj = models.Vault(
            vault=v,
            quote=quote,
            base=base,
            market=market,
            owner=ev.owner.lower(),
            name=ev.name,
            description=ev.description,
            social1=ev.social1,
            social2=ev.social2,
            social3=ev.social3,
            locked=bool(ev.locked),
            closed=bool(ev.closed),
            maxShares=int(ev.maxShares),
            circulatingShares=int(ev.circulatingShares),
            quoteDecimals=int(qd),
            baseDecimals=int(bd),
            timestamp=ts,
        )
        self.vaults[v] = vault_obj

        if v not in self.allVaults:
            self.allVaults.add(v)

        if v not in self.vaultBalancesDay:
            self.vaultBalancesDay[v] = deque()
        if v not in self.vaultBalancesWeek:
            self.vaultBalancesWeek[v] = deque()
        if v not in self.vaultBalancesMonth:
            self.vaultBalancesMonth[v] = deque()
        if v not in self.vaultBalancesAllTime:
            self.vaultBalancesAllTime[v] = deque()

        if v not in self.vaultToDeposits:
            self.vaultToDeposits[v] = []
        if v not in self.vaultToWithdraws:
            self.vaultToWithdraws[v] = []
        if v not in self.vaultToUsers:
            self.vaultToUsers[v] = {}

    def apply_vault_deposit(self, vault: str, ev: models.VaultDeposit) -> None:
        v = vault.lower()
        if v not in self.vaults:
            return

        arr = self.vaultToDeposits.setdefault(v, [])
        arr.append(
            models.VaultDeposit(
                user=ev.user.lower(),
                timestamp=int(ev.timestamp),
                quoteAmount=int(ev.quoteAmount),
                baseAmount=int(ev.baseAmount),
                shares=int(ev.shares),
                hash=ev.hash.lower(),
            )
        )

        self.vaults[v].circulatingShares += int(ev.shares)

        users = self.vaultToUsers.setdefault(v, {})
        ukey = ev.user.lower()
        u = users.get(ukey)

        if u is None:
            users[ukey] = models.VaultUser(
                address=ukey,
                vault=v,
                shares=int(ev.shares),
                deposits=1,
                withdraws=0,
                lastDeposit=int(ev.timestamp),
                lastWithdraw=0,
            )
        else:
            u.shares += int(ev.shares)
            u.deposits += 1
            u.lastDeposit = int(ev.timestamp)

        latest = self.vaultLatest.get(
            v, {"quote": 0, "base": 0, "ts": int(ev.timestamp)}
        )
        latest["quote"] += int(ev.quoteAmount)
        latest["base"] += int(ev.baseAmount)
        latest["ts"] = int(ev.timestamp)
        self.vaultLatest[v] = latest

        day_empty = len(self.vaultBalancesDay.get(v, [])) == 0
        week_empty = len(self.vaultBalancesWeek.get(v, [])) == 0
        month_empty = len(self.vaultBalancesMonth.get(v, [])) == 0
        all_empty = len(self.vaultBalancesAllTime.get(v, [])) == 0
        if day_empty or week_empty or month_empty or all_empty:
            self._seed_first_point(v, latest["ts"], latest["quote"], latest["base"])

    def apply_vault_withdraw(self, vault: str, ev: models.VaultWithdraw) -> None:
        v = vault.lower()
        if v not in self.vaults:
            return

        arr = self.vaultToWithdraws.setdefault(v, [])
        arr.append(
            models.VaultWithdraw(
                user=ev.user.lower(),
                timestamp=int(ev.timestamp),
                quoteAmount=int(ev.quoteAmount),
                baseAmount=int(ev.baseAmount),
                shares=int(ev.shares),
                hash=ev.hash.lower(),
            )
        )

        self.vaults[v].circulatingShares -= int(ev.shares)
        if self.vaults[v].circulatingShares < 0:
            self.vaults[v].circulatingShares = 0

        users = self.vaultToUsers.setdefault(v, {})
        ukey = ev.user.lower()
        u = users.get(ukey)
        if u is None:
            users[ukey] = models.VaultUser(
                address=ukey,
                vault=v,
                shares=max(0, -int(ev.shares)),
                deposits=0,
                withdraws=1,
                lastDeposit=0,
                lastWithdraw=int(ev.timestamp),
            )
            if users[ukey].shares < 0:
                users[ukey].shares = 0
        else:
            u.shares -= int(ev.shares)
            if u.shares < 0:
                u.shares = 0
            u.withdraws += 1
            u.lastWithdraw = int(ev.timestamp)

    def _vault_usd(self, vaddr: str, quote: int, base: int) -> float:
        v = self.vaults.get(vaddr)
        if not v:
            return 0.0
        q_price = self.token_price(v.quote.lower())
        b_price = self.token_price(v.base.lower())
        q_dec = int(v.quoteDecimals or 0)
        b_dec = int(v.baseDecimals or 0)
        usd_q = float(quote) / (10 ** q_dec) * q_price
        usd_b = float(base) / (10 ** b_dec) * b_price
        return usd_q + usd_b

    def _seed_first_point(self, vaddr: str, ts: int, quote: int, base: int) -> None:
        usd = self._vault_usd(vaddr, quote, base)
        row = {
            "block": 0,
            "timestamp": int(ts),
            "quoteBalance": int(quote),
            "baseBalance": int(base),
            "usdValue": float(usd),
        }
        if vaddr not in self.vaultBalancesDay:
            self.vaultBalancesDay[vaddr] = deque()
        if vaddr not in self.vaultBalancesWeek:
            self.vaultBalancesWeek[vaddr] = deque()
        if vaddr not in self.vaultBalancesMonth:
            self.vaultBalancesMonth[vaddr] = deque()
        if vaddr not in self.vaultBalancesAllTime:
            self.vaultBalancesAllTime[vaddr] = deque()

        if len(self.vaultBalancesDay[vaddr]) == 0:
            self.vaultBalancesDay[vaddr].append(row.copy())
        if len(self.vaultBalancesWeek[vaddr]) == 0:
            self.vaultBalancesWeek[vaddr].append(row.copy())
        if len(self.vaultBalancesMonth[vaddr]) == 0:
            self.vaultBalancesMonth[vaddr].append(row.copy())
        if len(self.vaultBalancesAllTime[vaddr]) == 0:
            self.vaultBalancesAllTime[vaddr].append(row.copy())

    # amm lp
    def _pool_tvl_usd(self, pool: models.AMMPool) -> float:
        q_price = self.token_price(pool.quote)
        b_price = self.token_price(pool.base)
        q_dec = int(pool.quoteDecimals or 0)
        b_dec = int(pool.baseDecimals or 0)

        usd_q = float(pool.reserveQuote) / (10 ** q_dec) * q_price
        usd_b = float(pool.reserveBase) / (10 ** b_dec) * b_price
        return usd_q + usd_b

    def _pool_trade_volume_usd(self, pool: models.AMMPool, dq: int, db: int) -> float:
        q_price = self.token_price(pool.quote)
        b_price = self.token_price(pool.base)
        q_dec = int(pool.quoteDecimals or 0)
        b_dec = int(pool.baseDecimals or 0)

        if dq > 0 and db < 0:
            if q_price <= 0:
                return 0.0
            return float(dq) / (10 ** q_dec) * q_price

        if db > 0 and dq < 0:
            if b_price <= 0:
                return 0.0
            return float(db) / (10 ** b_dec) * b_price

        return 0.0

    def apply_amm_sync(self, blk: int, ev: dict, ts: int) -> None:
        market = ev.get("market", "").lower()
        pool = self.ammPools.get(market)
        if pool is None:
            return

        new_q = int(ev.get("reserveQuote", 0))
        new_b = int(ev.get("reserveBase", 0))

        old_q = pool.reserveQuote
        old_b = pool.reserveBase

        if old_q == 0 and old_b == 0:
            pool.reserveQuote = new_q
            pool.reserveBase = new_b
            pool.tvlUsd = Decimal(self._pool_tvl_usd(pool))
            return

        dq = new_q - old_q
        db = new_b - old_b

        pool.reserveQuote = new_q
        pool.reserveBase = new_b
        pool.tvlUsd = Decimal(self._pool_tvl_usd(pool))

        if dq == 0 and db == 0:
            return

        if (dq > 0 and db > 0) or (dq < 0 and db < 0):
            return

        vol_usd = self._pool_trade_volume_usd(pool, dq, db)
        if vol_usd <= 0:
            return

        dq_events = self.ammEvents24h.setdefault(market, deque())
        dq_events.append({"timestamp": int(ts), "volumeUsd": float(vol_usd)})

        cutoff = max(0, int(ts) - 24 * 3600)
        total = self.ammVolume24h.get(market, 0.0) + float(vol_usd)
        while dq_events and int(dq_events[0]["timestamp"]) < cutoff:
            total -= float(dq_events[0]["volumeUsd"])
            dq_events.popleft()

        total = max(total, 0.0)
        self.ammVolume24h[market] = total
        pool.volume24hUsd = Decimal(total)

    # launchpad
    def apply_token_created(self, blk: int, ev: dict, ts: int, _log_addr: str) -> None:
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
                lp.last_price_native = ev.get("last_price_native", Decimal("0.00008387696"))
        
        if creator:
            u = self.launchpad_users.get(creator)
            if u is None:
                u = models.LaunchpadUser(address=creator)
                self.launchpad_users[creator] = u
            u.tokens_created += 1
            
    def apply_launchpad_trade(self, ev: dict, blk: int, ts: int, _log_addr: str) -> None:
        print(ev)
        
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
        
        lp = self.launchpad_tokens.get(token)
        if lp is None:
            print("not a token", self.launchpad_tokens)
            return
        
        try:
            price_native = Decimal(ev.get("native_reserve")) / Decimal(ev.get("token_reserve"))
        except Exception:
            price_native = Decimal(0)
            
        if lp.source == 1:
            price_native = price_native * Decimal("0.05")
    
        lp.last_price_native = price_native
        
        if (not lp.approaching_75) and ev.get("native_reserve") >= 2500000000000000000000:
            lp.approaching_75 = True
            lp.approaching_75_block = blk
            lp.approaching_75_at = ts
        elif (lp.approaching_75) and ev.get("native_reserve") < 2500000000000000000000:
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

        mon_price = self.tokenToPrice.get("0x760afe86e5de5fa0ee542fc7b7b713e1c5425701", Decimal(0))
        if mon_price > 0:
            volume_usd_trade = (Decimal(native_amt) / (Decimal(10) ** 18)) * mon_price
            lp.volume_usd += volume_usd_trade
            lp.fees_usd += volume_usd_trade * Decimal("0.01")

        lu = self.launchpad_users.get(user)
        if lu is None:
            lu = models.LaunchpadUser(address=user)
            self.launchpad_users[user] = lu
        lu.total_trades += 1

        key = (user, token)
        pos = self.launchpad_positions.get(key)
        if pos is None:
            pos = models.LaunchpadPosition(user=user, token=token)
            self.launchpad_positions[key] = pos

        pos.trade_count += 1
        if is_buy:
            pos.buy_count += 1
            pos.token_bought += token_amt
            pos.native_spent += native_amt
            pos.balance_token += token_amt
        else:
            pos.sell_count += 1
            pos.token_sold += token_amt
            pos.native_received += native_amt
            pos.balance_token -= token_amt
            if pos.balance_token < 0:
                pos.balance_token = 0

        old_realized = pos.realized_pnl_native
        realized_native = pos.native_received - pos.native_spent
        pos.realized_pnl_native = Decimal(realized_native)

        delta = pos.realized_pnl_native - old_realized
        lu.total_realized_pnl_native += delta
        
        trades = self.launchpad_trades.setdefault(token, [])
        trades.append(
            models.LaunchpadTrade(
                block_number=blk,
                timestamp=int(ts),
                token=token,
                user=user,
                is_buy=is_buy,
                native_amount=int(native_amt),
                token_amount=int(token_amt),
                price_native=lp.last_price_native,
            )
        )
        if len(trades) > 500000:
            trades[:] = trades[-500000:]

    def apply_migrated(self, blk: int, ts: int, ev: dict, _log_addr: str) -> None:
        token = ev.get("token", "").lower()
        if not token:
            return

        lp = self.launchpad_tokens.get(token)
        if lp is None:
            return

        lp.migrated = True
        lp.migrated_block = blk
        lp.migrated_at = ts

        creator = lp.creator.lower() if lp.creator else ""
        if creator:
            u = self.launchpad_users.get(creator)
            if u is None:
                u = models.LaunchpadUser(address=creator)
                self.launchpad_users[creator] = u
            u.tokens_graduated += 1
