from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Deque, List, Tuple
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

@dataclass(slots=True)
class _VaultSnap:
    ts: int
    block: int
    mon_bal: int
    quote_bal: int
    base_bal: int
    total_shares: int = 0

@dataclass(slots=True)
class _BinPoint:
    ts: int
    mon_bal: int
    quote_bal: int
    base_bal: int
    total_shares: int

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

    def ts(self, block: int) -> int:
        if block in self._ts_by_block:
            return self._ts_by_block[block]
        try:
            blk = self._rpc("eth_getBlockByNumber", [hex(block), False])
            ts = int(blk["timestamp"], 16)
            self._ts_by_block[block] = ts
            return ts
        except Exception:
            _, head_ts = self.head()
            return head_ts

class State:
    def __init__(self) -> None:
        self._events: Dict[str, Deque[_Evt]] = {}
        self._created_at_block: Dict[str, int] = {}
        self._bt = _BlockTimeCache()
        self._mon_usd: Decimal = Decimal(0)
        self._vault_meta: Dict[str, Tuple[str, str]] = {}
        self._vault_last_min: Dict[str, _VaultSnap] = {}
        self._vault_bins: Dict[str, Dict[int, Deque[_BinPoint]]] = {}
        self._vault_bin_sizes = (3600, 21600, 43200, 86400)
        self._vault_retention = {3600: 86400, 21600: 7*86400, 43200: 14*86400, 86400: 30*86400}

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
    
    def apply_trade(self, ev: models.Trade, _log_addr: str) -> None:
        mon_usd_addr = h.CONTRACTS.get("MON_USD_PAIR", "").lower()
        if not mon_usd_addr:
            return
        if ev.market.lower() != mon_usd_addr:
            return

        if ev.end_price:
            try:
                self._mon_usd = Decimal(ev.end_price) / 1e9
                return
            except Exception:
                pass

        ain, aout = int(ev.amount_in), int(ev.amount_out)
        if ev.is_buy:
            if ain != 0:
                self._mon_usd = Decimal(aout) / Decimal(ain)
        else:
            if aout != 0:
                self._mon_usd = Decimal(ain) / Decimal(aout)

    def mon_usd_price(self) -> float:
        return float(self._mon_usd) if self._mon_usd else 0.0

    def register_vault(self, vault: str, quote: str, base: str) -> None:
        v = vault.lower()
        self._vault_meta.setdefault(v, (quote.lower(), base.lower()))
        
        if v not in self._vault_bins:
            self._vault_bins[v] = {b: deque() for b in self._vault_bin_sizes}

    def apply_vault_snapshot(
        self, vault: str, blk: int, ts: int,
        mon_bal: int, quote_bal: int, base_bal: int,
        total_shares: int = 0
    ) -> None:
        v = vault.lower()
        snap = _VaultSnap(ts, blk, mon_bal, quote_bal, base_bal, total_shares)
        self._vault_last_min[v] = snap
        
        for b in self._vault_bin_sizes:
            bucket_ts = ts - (ts % b)
            dq = self._vault_bins.setdefault(v, {}).setdefault(b, deque())
            
            if dq and dq[-1].ts == bucket_ts:
                dq[-1] = _BinPoint(bucket_ts, mon_bal, quote_bal, base_bal, total_shares)
            else:
                dq.append(_BinPoint(bucket_ts, mon_bal, quote_bal, base_bal, total_shares))
                
            horizon = self._vault_retention[b]
            cut = ts - horizon
            
            while dq and dq[0].ts < cut:
                dq.popleft()

    def vault_meta(self) -> Dict[str, Tuple[str, str]]:
        return dict(self._vault_meta)

    def vault_latest_minute(self, vault: str) -> Dict[str, int]:
        s = self._vault_last_min.get(vault.lower())
        
        if not s: return {}
        
        return {
            "ts": s.ts, 
            "block": s.block, 
            "mon_bal": s.mon_bal,
            "quote_bal": s.quote_bal, 
            "base_bal": s.base_bal,
            "total_shares": s.total_shares
        }

    def vault_series(self, vault: str, horizon: str) -> List[Dict[str, int]]:
        map_bin = {"1d":3600, "7d":21600, "14d":43200, "30d":86400}
        b = map_bin[horizon]
        dq = self._vault_bins.get(vault.lower(), {}).get(b, deque())
        return [
            {
                "ts": p.ts, 
                "mon_bal": p.mon_bal, 
                "quote_bal": p.quote_bal,
                "base_bal": p.base_bal, 
                "total_shares": p.total_shares
            } 
            for p in dq
        ]

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