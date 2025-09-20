from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Deque, List, Tuple
from collections import deque
from decimal import Decimal, getcontext
import json
import urllib.request
import time
import models

getcontext().prec = 50

INTERVALS = (300, 3600, 21600, 86400)
LABEL = {300:"5m", 3600:"1h", 21600:"6h", 86400:"24h"}

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

    def apply_launchpad_trade(self, ev: models.LaunchpadTrade, _log_addr: str) -> None:
        token = ev.token.lower()

        native_amt = ev.amount_in if ev.is_buy else ev.amount_out
        token_amt  = ev.amount_out if ev.is_buy else ev.amount_in

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
        n = len(items)

        last_price = items[-1].native_per_token if n > 0 else None

        prune_idx = -1
        last_pre_24h_idx = -1
        for idx in range(n - 1, -1, -1):
            e = items[idx]
            ts = self._bt.ts(e.block)
            age = head_ts - ts

            if last_pre_24h_idx == -1 and ts <= head_ts - 86400:
                last_pre_24h_idx = idx

            oldest_price = items[0].native_per_token
            for h in INTERVALS:
                if res[h]["start_price_native"] is None:
                    res[h]["start_price_native"] = oldest_price

            for h in INTERVALS:
                if age <= h:
                    if e.is_buy:
                        res[h]["buy_cnt"] += 1
                        res[h]["buy_vol_native"] += e.native_vol
                    else:
                        res[h]["sell_cnt"] += 1
                        res[h]["sell_vol_native"] += e.native_vol
                else:
                    if res[h]["start_price_native"] is None:
                        res[h]["start_price_native"] = e.native_per_token

            if prune_idx == -1 and age > 86400:
                prune_idx = idx

        for h in INTERVALS:
            res[h]["total_vol_native"] = res[h]["buy_vol_native"] + res[h]["sell_vol_native"]
            res[h]["last_price_native"] = float(last_price) if last_price is not None else None
            sp = res[h]["start_price_native"]
            lp = last_price
            if sp is not None and lp is not None and sp != 0:
                res[h]["change_pct"] = float(((lp - sp) / sp) * Decimal(100))
            if isinstance(sp, Decimal):
                res[h]["start_price_native"] = float(sp)

        if prune_idx != -1:
            end = max(0, min(prune_idx, last_pre_24h_idx))
            for _ in range(end):
                dq.popleft()

        return res

    def snapshot(self, token: str) -> Dict[int, Dict[str, int]]:
        return self._compute_token_snapshot(token)

    def batch_snapshot(self, tokens: List[str]) -> Dict[str, Dict[int, Dict[str, int]]]:
        return {t.lower(): self._compute_token_snapshot(t) for t in tokens}

    def debug_tokens(self) -> Dict[str, int]:
        return {t: len(q) for t, q in self._events.items()}