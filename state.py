from __future__ import annotations
from dataclasses import dataclass, is_dataclass, asdict
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

SNAPSHOT_FILE = os.getenv("CRYSTAL_STATE_SNAPSHOT", "state_snapshot.json")


class _BlockTimeCache:
    def __init__(self) -> None:
        self._ts_by_block: Dict[int, int] = {}
        self._last_head_block: int | None = None
        self._last_head_ts: int | None = None

    def _rpc(self, method: str, params: list) -> dict:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            RPC_HTTP, data=payload, headers={"Content-Type": "application/json"}
        )
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


def _json_encode(obj):
    if isinstance(obj, Decimal):
        return {"__decimal__": str(obj)}
    if isinstance(obj, deque):
        return {"__deque__": [_json_encode(x) for x in obj]}
    if isinstance(obj, set):
        return {"__set__": [_json_encode(x) for x in obj]}
    if is_dataclass(obj):
        data = asdict(obj)
        return {
            "__dataclass__": obj.__class__.__name__,
            "data": {k: _json_encode(v) for k, v in data.items()},
        }
    if hasattr(obj, "dict") and callable(getattr(obj, "dict", None)):
        data = obj.dict()
        return {
            "__model__": obj.__class__.__name__,
            "data": {k: _json_encode(v) for k, v in data.items()},
        }
    if isinstance(obj, dict):
        return {k: _json_encode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_encode(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_encode(v) for v in obj]
    return obj


def _lookup_class(name: str):
    cls = getattr(models, name, None)
    if cls is not None:
        return cls
    mod = sys.modules.get(__name__)
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return None


def _json_decode(obj):
    if isinstance(obj, dict):
        if "__decimal__" in obj:
            return Decimal(obj["__decimal__"])
        if "__set__" in obj:
            return set(_json_decode(v) for v in obj["__set__"])
        if "__deque__" in obj:
            return deque(_json_decode(v) for v in obj["__deque__"])
        if "__dataclass__" in obj or "__model__" in obj:
            cname = obj.get("__dataclass__") or obj.get("__model__")
            data_raw = obj.get("data", {})
            data = {k: _json_decode(v) for k, v in data_raw.items()}
            cls = _lookup_class(cname) if cname else None
            if cls is None:
                return data
            try:
                return cls(**data)
            except Exception:
                return data
        return {k: _json_decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_decode(v) for v in obj]
    return obj


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

        self.ammPools: Dict[str, models.AMMPool] = {}
        self.ammEvents24h: Dict[str, Deque[dict]] = {}
        self.ammVolume24h: Dict[str, float] = {}
        self.ammHistory: Dict[str, List[dict]] = {}

        self.launchpad_tokens: Dict[str, models.LaunchpadToken] = {}
        self.launchpad_users: Dict[str, models.LaunchpadUser] = {}
        self.launchpad_positions: Dict[tuple[str, str], models.LaunchpadPosition] = {}

        self.last_processed_block: int | None = None

        if False:
            self.seed_single_market()
            self.sweep()

    # snapshotting/persistence, db related things
    def _to_snapshot_dict(self) -> dict:
        return {
            "version": 1,
            "block_time_cache": {
                "ts_by_block": self._bt._ts_by_block,
                "last_head_block": self._bt._last_head_block,
                "last_head_ts": self._bt._last_head_ts,
            },
            "events": self._events,
            "created_at_block": self._created_at_block,
            "addressToMarket": self.addressToMarket,
            "tokenToPrice": self.tokenToPrice,
            "allVaults": list(self.allVaults),
            "vaults": self.vaults,
            "vaultBalancesDay": self.vaultBalancesDay,
            "vaultBalancesWeek": self.vaultBalancesWeek,
            "vaultBalancesMonth": self.vaultBalancesMonth,
            "vaultBalancesAllTime": self.vaultBalancesAllTime,
            "vaultToDeposits": self.vaultToDeposits,
            "vaultToWithdraws": self.vaultToWithdraws,
            "vaultToUsers": self.vaultToUsers,
            "vaultLatest": self.vaultLatest,
            "ammPools": self.ammPools,
            "ammEvents24h": self.ammEvents24h,
            "ammVolume24h": self.ammVolume24h,
            "ammHistory": self.ammHistory,
            "last_processed_block": self.last_processed_block,
        }

    def save_to_file(self, path: str = SNAPSHOT_FILE) -> None:
        data = self._to_snapshot_dict()
        safe = _json_encode(data)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(safe, f)
        os.replace(tmp_path, path)

    @classmethod
    def load_from_file(cls, path: str = SNAPSHOT_FILE) -> State | None:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = _json_decode(raw)
        self = cls()
        btc = data.get("block_time_cache", {})
        self._bt = _BlockTimeCache()
        self._bt._ts_by_block = btc.get("ts_by_block", {})
        self._bt._last_head_block = btc.get("last_head_block")
        self._bt._last_head_ts = btc.get("last_head_ts")

        self._events = data.get("events", {})
        self._created_at_block = data.get("created_at_block", {})

        self.addressToMarket = data.get("addressToMarket", {})
        self.tokenToPrice = data.get("tokenToPrice", {})

        self.tokenGraph = {}
        for mi in self.addressToMarket.values():
            try:
                base = mi.baseAddress.lower()
                quote = mi.quoteAddress.lower()
            except Exception:
                continue
            self.tokenGraph.setdefault(base, []).append(mi)
            self.tokenGraph.setdefault(quote, []).append(mi)

        self.vaults = data.get("vaults", {})
        self.allVaults = set(data.get("allVaults", list(self.vaults.keys())))
        self.vaultBalancesDay = data.get("vaultBalancesDay", {})
        self.vaultBalancesWeek = data.get("vaultBalancesWeek", {})
        self.vaultBalancesMonth = data.get("vaultBalancesMonth", {})
        self.vaultBalancesAllTime = data.get("vaultBalancesAllTime", {})
        self.vaultToDeposits = data.get("vaultToDeposits", {})
        self.vaultToWithdraws = data.get("vaultToWithdraws", {})
        self.vaultToUsers = data.get("vaultToUsers", {})
        self.vaultLatest = data.get("vaultLatest", {})

        self.ammPools = data.get("ammPools", {})
        self.ammEvents24h = data.get("ammEvents24h", {})
        self.ammVolume24h = data.get("ammVolume24h", {})
        self.ammHistory = data.get("ammHistory", {})

        self.last_processed_block = data.get("last_processed_block")

        return self

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

        self.apply_market_created(mi, mi.market)
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
                )

    def block_ts(self, block: int) -> int:
        t = self._bt.ts(block)
        if t is not None:
            return t
        return self._bt._last_head_ts or 0

    def head_block_and_ts(self) -> tuple[int | None, int | None]:
        return self._bt._last_head_block, self._bt._last_head_ts

    # market creation, prices
    def apply_market_created(self, ev: models.MarketInfo, _log_addr: str) -> None:
        if not ev.isCanonical:
            return

        base = ev.baseAddress.lower()
        quote = ev.quoteAddress.lower()
        market = ev.market.lower()

        self.addressToMarket[market] = ev

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
                )
                self.ammEvents24h[market] = deque()
                self.ammVolume24h[market] = 0.0
                self.ammHistory[market] = []

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
        """
        handle launchpad TokenCreated events.

        expected ev shape (from parse_token_created):

        {
            "token": str,
            "creator": str,
            "name": str,
            "symbol": str,
            "metadata_cid": str,
            "description": str,
            "social1": str,
            "social2": str,
            "social3": str,
            "social4": str,
            # optional:
            # "source": int  # 0 = own router, 1 = nad.fun, 2 = printr
        }
        """
        token = ev.get("token", "").lower()
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

        existing = self.launchpad_tokens.get(token)

        if existing is None:
            # create a fresh token record from metadata
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
            # fill non-init fields
            if hasattr(lp, "created_block"):
                lp.created_block = blk
            if hasattr(lp, "source"):
                lp.source = source
            self.launchpad_tokens[token] = lp
        else:
            # backfill any missing metadata; do NOT overwrite non-empty values
            if not getattr(existing, "creator", "") and creator:
                existing.creator = creator
            if not getattr(existing, "name", "") and name:
                existing.name = name
            if not getattr(existing, "symbol", "") and symbol:
                existing.symbol = symbol
            if not getattr(existing, "metadata_cid", "") and metadata_cid:
                existing.metadata_cid = metadata_cid
            if not getattr(existing, "description", "") and description:
                existing.description = description
            if not getattr(existing, "social1", "") and social1:
                existing.social1 = social1
            if not getattr(existing, "social2", "") and social2:
                existing.social2 = social2
            if not getattr(existing, "social3", "") and social3:
                existing.social3 = social3
            if not getattr(existing, "social4", "") and social4:
                existing.social4 = social4

            if hasattr(existing, "created_block") and getattr(existing, "created_block", 0) == 0:
                existing.created_block = blk
            if hasattr(existing, "source") and getattr(existing, "source", 0) == 0:
                existing.source = source

        # cache block timestamp in case we need it later for time-based analytics
        if ts:
            self._bt.note(blk, int(ts))


    def apply_launchpad_trade(self, blk: int, ev: dict, ts: int, _log_addr: str) -> None:
        """
        handle LaunchpadTrade events (both from our router and future integrations).

        expected ev shape (from parse_launchpad_trade):

        {
            "token": str,
            "user": str,
            "is_buy": bool,
            "amount_in": int,
            "amount_out": int,
            "native_reserve": int,
            "token_reserve": int,
            # optional:
            # "source": int  # if you ever need per-trade source
        }
        """
        token = ev.get("token", "").lower()
        user = ev.get("user", "").lower()
        if not token or not user:
            return

        is_buy = bool(ev.get("is_buy", False))
        amount_in = int(ev.get("amount_in", 0) or 0)
        amount_out = int(ev.get("amount_out", 0) or 0)

        # normalize direction:
        # - native = what the user pays on buy, what they receive on sell
        # - token_amt = what the user receives on buy, what they pay on sell
        native_amt = amount_in if is_buy else amount_out
        token_amt = amount_out if is_buy else amount_in

        if token_amt > 0:
            try:
                price = Decimal(native_amt) / Decimal(token_amt)
            except Exception:
                price = Decimal(0)
        else:
            price = Decimal(0)

        # ensure token record exists; if token_created fired already, this won't overwrite metadata
        lp = self.launchpad_tokens.get(token)
        if lp is None:
            lp = models.LaunchpadToken(
                token=token,
                creator="",
                name="",
                symbol="",
                metadata_cid="",
                description="",
                social1="",
                social2="",
                social3="",
                social4="",
            )
            if hasattr(lp, "created_block"):
                lp.created_block = blk
            self.launchpad_tokens[token] = lp

        # update last seen price
        if hasattr(lp, "last_price_native"):
            lp.last_price_native = price

        # you can later add more per-token aggregates here (volumes, fees, tx counts, etc.)

        # ensure user record
        lu = self.launchpad_users.get(user)
        if lu is None:
            lu = models.LaunchpadUser(address=user)
            self.launchpad_users[user] = lu
        lu.total_trades += 1

        # per-user, per-token position
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

        # pnl logic will be added later; for now we leave realized_pnl_native untouched

        if ts:
            self._bt.note(blk, int(ts))


    def apply_migrated(self, blk: int, ev: dict, ts: int, _log_addr: str) -> None:
        token = ev.get("token", "").lower()
        if not token:
            return

        lp = self.launchpad_tokens.get(token)
        if lp is None:
            return
        
        if hasattr(lp, "migrated"):
            lp.migrated = True
        if hasattr(lp, "migrated_block") and getattr(lp, "migrated_block", 0) == 0:
            lp.migrated_block = blk

        if ts:
            self._bt.note(blk, int(ts))