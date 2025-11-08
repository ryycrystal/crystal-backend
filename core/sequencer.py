from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Callable, Optional

from core import chain as h
import modules.launchpad as lp
import models
import state as _st

class Sequencer:
    def __init__(self, global_state: _st.State) -> None:
        self._state = global_state
        self._logs_by_block: Dict[int, List[dict]] = defaultdict(list)
        self._ready_blocks: set[int] = set()
        self._next_block: int | None = None
        self._on_block: Optional[Callable[[int], None]] = None
    
    def set_on_block(self, fn: Callable[[int], None]) -> None:
        self._on_block = fn

    def add_log(self, raw_log: dict) -> None:
        blk = int(raw_log["blockNumber"], 16) if isinstance(raw_log["blockNumber"], str) else raw_log["blockNumber"]
        self._logs_by_block[blk].append(raw_log)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    def note_block(self, blk: int) -> None:
        self._ready_blocks.add(blk)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    def _drain(self) -> None:
        while self._next_block is not None and self._next_block in self._ready_blocks:
            logs = self._logs_by_block.pop(self._next_block, [])
            self._ready_blocks.discard(self._next_block)
            self._process_block(self._next_block, logs)
            if self._on_block:
                try:
                    self._on_block(self._next_block)
                except Exception as e:
                    print(f"[SQ][persist][error] {e!r}")
            self._next_block += 1

    def _process_block(self, blk: int, logs: List[dict]):
        counts = {"TC": 0, "LT": 0, "TR": 0, "VC": 0}
        seen = set()

        for log in logs:
            txh = log.get("transactionHash")
            li = log.get("logIndex")
            lii = int(li, 16) if isinstance(li, str) else int(li or 0)
            uid = (txh, lii)
            
            if uid in seen:
                continue
            seen.add(uid)

            tag = h.EVENT_SIGS.get(log["topics"][0].lower())
            if tag:
                counts[tag] += 1

            if tag not in ("LT", "TC", "TR", "VC"):
                continue
            
            parsed = h.PARSERS[tag](log["address"].lower(), log["topics"], log["data"][2:])
            if tag == "LT":
                ev = self._to_launchpad_trade(parsed, blk)
                self._state.apply_launchpad_trade(ev, log["address"].lower())
            elif tag == "TC":
                ev = self._to_token_created(parsed, blk)
                self._state.apply_token_created(ev, log["address"].lower())
            elif tag == "TR":
                ev = self._to_trade(parsed, blk)
                self._state.apply_trade(ev, log["address"].lower())
            elif tag == "VC":
                ev = self._to_vault_created(parsed, blk)
                self._state.register_vault(ev.vault, ev.quote, ev.base)
        
        print(f"[SQ] {blk}: TC {counts['TC']} LT {counts['LT']} TR {counts['TR']} VC {counts['VC']}")

    @staticmethod
    def _to_launchpad_trade(d: dict, blk: int) -> models.LaunchpadTrade:
        return models.LaunchpadTrade(
            block_number=blk,
            token=d.get("token", d.get("caller","")).lower(),
            user=d.get("user","").lower(),
            is_buy=bool(d["is_buy"]) if "is_buy" in d else bool(d.get("side", 0)),
            amount_in=int(d.get("amount_in", 0)),
            amount_out=int(d.get("amount_out", 0)),
            native_reserve=int(d.get("native_reserve", 0)),
            token_reserve=int(d.get("token_reserve", 0)),
        )

    @staticmethod
    def _to_token_created(d: dict, blk: int) -> models.TokenCreated:
        return models.TokenCreated(
            block_number=blk,
            token=d.get("token", d.get("caller","")).lower(),
            creator=d.get("creator","").lower(),
        )
    
    @staticmethod
    def _to_trade(d: dict, blk: int) -> models.Trade:
        return models.Trade(
            block_number=blk,
            market=d.get("market", "").lower(),
            user=d.get("user", "").lower(),
            is_buy=bool(d.get("is_buy", False)),
            amount_in=int(d.get("amount_in", 0)),
            amount_out=int(d.get("amount_out", 0)),
            start_price=int(d.get("start_price", 0)),
            end_price=int(d.get("end_price", 0)),
        )
    
    @staticmethod
    def _to_vault_created(d: dict, blk: int) -> models.VaultCreated:
        return models.VaultCreated(
            block_number=blk,
            vault=d.get("vault","").lower(),
            quote=d.get("quote","").lower(),
            base=d.get("base","").lower(),
        )

SEQUENCER = Sequencer(_st.State())