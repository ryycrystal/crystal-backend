from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Deque, List, Tuple, Set
from types import SimpleNamespace
from collections import deque
from decimal import Decimal, getcontext
from core import chain as h
import json
import urllib.request
import time
import models

getcontext().prec = 50

INTERVALS = (300, 3600, 21600, 86400)
LABEL = { 300:"5m", 3600:"1h", 21600:"6h", 86400:"24h" }

RPC_HTTP = "https://testnet-rpc.monad.xyz"

@dataclass(slots=True)
class _Evt:
    block: int
    is_buy: bool
    native_vol: int
    token_amt: int
    native_per_token: Decimal

class _BlockTimeCache:
    def __init__(self) -> None:
        self._ts_by_block: Dict[int, int] = {}
        self._last_head_block: int | None = None
        self._last_head_ts: int | None = None

    def _rpc(self, method: str, params: list) -> dict:
        payload = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
        req = urllib.request.Request(RPC_HTTP, data=payload, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())["result"]

    def head(self) -> Tuple[int, int]:
        try:
            head_hex = self._rpc("eth_blockNumber", [])
            head_num = int(head_hex, 16)
            if self._last_head_block == head_num and self._last_head_ts is not None:
                return head_num, self._last_head_ts
            blk = self._rpc("eth_getBlockByNumber", [hex(head_num), False])
            ts = int(blk["timestamp"], 16)
            self._last_head_block = head_num
            self._last_head_ts = ts
            self._ts_by_block[head_num] = ts
            return head_num, ts
        except Exception:
            now = int(time.time())
            return self._last_head_block or 0, self._last_head_ts or now

    def ts(self, block: int) -> int | None:
        return self._ts_by_block.get(block)
    
    def note(self, block: int, ts: int) -> None:
        self._ts_by_block[block] = ts
        self._last_head_block = block
        self._last_head_ts = ts

class State:
    def __init__(self) -> None:
        self._events: Dict[str, Deque[_Evt]] = {}
        self._created_at_block: Dict[str, int] = {}
        self._bt = _BlockTimeCache()

        self.addressToMarket: Dict[str, models.MarketInfo] = {}
        self.tokenToPrice: Dict[str, Decimal] = {}
        self.tokenGraph: Dict[str, List[models.MarketInfo]] = {}
        self.tokenToPrice["0xf817257fed379853cde0fa4f97ab987181b1e5ea"] = Decimal(1)
        
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
        
        if True: # for testing
            self.seed_single_market()
            self.sweep()
    
    def seed_single_market(self) -> None:
        m = {
            "id": "0xd91708c758a73590df354bda6b0b137564f54a0a",
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

        self.apply_market_created(mi, mi.market)
        self.addressToMarket[mi.market] = mi


    def block_ts(self, block: int) -> int:
        t = self._bt.ts(block)
        if t is not None:
            return t
        return self._bt._last_head_ts or 0

    def head_block_and_ts(self) -> tuple[int | None, int | None]:
        return self._bt._last_head_block, self._bt._last_head_ts


    def apply_market_created(self, ev: models.MarketInfo, _log_addr: str) -> None:
        if not ev.isCanonical:
            return
        
        base = ev.baseAddress.lower()
        quote = ev.quoteAddress.lower()
        market = ev.market.lower()
        
        self.addressToMarket[market] = ev
        
        try:
            if getattr(ev, "price", None) is None:
                ev.price = Decimal(0)
        except Exception:
            pass
    
        lst_base = self.tokenGraph.setdefault(base, [])
        lst_base.append(ev)
        
        lst_quote = self.tokenGraph.setdefault(quote, [])
        lst_quote.append(ev)
    
    def apply_trade(self, ev: models.Trade, _log_addr: str) -> None:
        market = ev.market.lower()
        mi = self.addressToMarket.get(market)
        if mi is None:
            return
        
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
        arr.append(models.VaultDeposit(
            user=ev.user.lower(),
            timestamp=int(ev.timestamp),
            quoteAmount=int(ev.quoteAmount),
            baseAmount=int(ev.baseAmount),
            shares=int(ev.shares),
            hash=ev.hash.lower(),
        ))
        
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
                lastWithdraw=0
            )
        else:
            u.shares += int(ev.shares)
            u.deposits += 1
            u.lastDeposit = int(ev.timestamp)
        
        latest = self.vaultLatest.get(v, {"quote": 0, "base": 0, "ts": int(ev.timestamp)})
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
        
    def apply_vault_withdraw(self, vault:str, ev: models.VaultWithdraw) -> None:
        v = vault.lower()
        if v not in self.vaults:
            return
        
        arr = self.vaultToWithdraws.setdefault(v, [])
        arr.append(models.VaultWithdraw(
            user=ev.user.lower(),
            timestamp=int(ev.timestamp),
            quoteAmount=int(ev.quoteAmount),
            baseAmount=int(ev.baseAmount),
            shares=int(ev.shares),
            hash=ev.hash.lower(),
        ))
        
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
                lastWithdraw=int(ev.timestamp)
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
        q_price = self.token_price(v.quote)
        b_price = self.token_price(v.base)
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


    def apply_launchpad_trade(self, ev: models.LaunchpadTrade, _log_addr: str) -> None:
        token = ev.token.lower()

        native_amt = ev.amount_in if ev.is_buy else ev.amount_out
        token_amt = ev.amount_out if ev.is_buy else ev.amount_in

        if token_amt <= 0:
            price = Decimal(0)
        else:
            price = Decimal(native_amt) / Decimal(token_amt)

        q = self._events.setdefault(token, deque())
        q.append(_Evt(
            block=ev.block_number,
            is_buy=ev.is_buy,
            native_vol=int(native_amt),
            token_amt=int(token_amt),
            native_per_token=price,
        ))

    def apply_token_created(self, ev: models.TokenCreated, _log_addr: str) -> None:
        token = ev.token.lower()
        self._created_at_block.setdefault(token, ev.block_number)
        self._events.setdefault(token, deque())
    
    def _compute_token_snapshot(self, token: str) -> dict[int, dict]:
        token = token.lower()
        dq = self._events.get(token)
        res = {h: {
            "buy_cnt": 0,
            "sell_cnt": 0,
            "buy_vol_native": 0,
            "sell_vol_native": 0,
            "total_vol_native": 0,
            "start_price_native": None,
            "last_price_native": None,
            "change_pct": None,
        } for h in INTERVALS}

        if not dq:
            return res

        _, head_ts = self._bt.head()
        items = list(dq)
        last_price = items[-1].native_per_token if items else None

        cutoffs = {h: head_ts - h for h in INTERVALS}

        cutoff_24h = cutoffs[86400]
        prune_n = 0
        for e in items:
            ts = self._bt.ts(e.block)
            if ts < cutoff_24h:
                prune_n += 1
            else:
                break
        for _ in range(prune_n):
            dq.popleft()

        if prune_n:
            items = list(dq)
            if not items:
                return res
            last_price = items[-1].native_per_token

        for e in items:
            ts = self._bt.ts(e.block)
            age = head_ts - ts
            for h in INTERVALS:
                if age <= h:
                    if res[h]["start_price_native"] is None:
                        res[h]["start_price_native"] = e.native_per_token
                    if e.is_buy:
                        res[h]["buy_cnt"] += 1
                        res[h]["buy_vol_native"] += e.native_vol
                    else:
                        res[h]["sell_cnt"] += 1
                        res[h]["sell_vol_native"] += e.native_vol

        for h in INTERVALS:
            res[h]["total_vol_native"] = res[h]["buy_vol_native"] + res[h]["sell_vol_native"]
            res[h]["last_price_native"] = float(last_price) if last_price is not None else None

            sp = res[h]["start_price_native"]
            lp = last_price
            if sp is not None and lp is not None:
                sp_val = float(sp) if not isinstance(sp, (int, float)) else sp
                lp_val = float(lp) if not isinstance(lp, (int, float)) else lp
                if sp_val != 0:
                    res[h]["change_pct"] = ((lp_val - sp_val) / sp_val) * 100.0

            if sp is not None and not isinstance(sp, (int, float)):
                res[h]["start_price_native"] = float(sp)

        return res

    def snapshot(self, token: str) -> Dict[int, Dict[str, int]]:
        return self._compute_token_snapshot(token)

    def batch_snapshot(self, tokens: List[str]) -> Dict[str, Dict[int, Dict[str, int]]]:
        return {t.lower(): self._compute_token_snapshot(t) for t in tokens}

    def debug_tokens(self) -> Dict[str, int]:
        return {t: len(q) for t, q in self._events.items()}