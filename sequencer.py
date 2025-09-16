from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

import helpers as h
import models
import state as _st


class Sequencer:
    def __init__(self, global_state: _st.State) -> None:
        self._state = global_state
        self._logs_by_block: Dict[int, List[dict]] = defaultdict(list)
        self._ready_blocks: set[int] = set()
        self._next_block: int | None = None

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
            self._next_block += 1

    def _process_block(self, blk: int, logs: List[dict]):
        counts = {"TC": 0, "LT": 0}
        seen = set()

        for log in logs:
            txh = log.get("transactionHash")
            li  = log.get("logIndex")
            lii = int(li, 16) if isinstance(li, str) else int(li or 0)
            uid = (txh, lii)
            
            if uid in seen:
                continue
            seen.add(uid)

            tag = h.EVENT_SIGS.get(log["topics"][0].lower())
            if tag:
                counts[tag] += 1

            if tag not in ("LT", "TC"):
                continue
            
            parsed = h.PARSERS[tag](log["address"].lower(), log["topics"], log["data"][2:])
            if tag == "LT":
                ev = self._to_launchpad_trade(parsed, blk)
                self._state.apply_launchpad_trade(ev, log["address"].lower())
            else:
                ev = self._to_token_created(parsed, blk)
                self._state.apply_token_created(ev, log["address"].lower())
        
        print(f"[SQ] {blk}: TC {counts['TC']}  LT {counts['LT']}")

    @staticmethod
    def _to_launchpad_trade(d: dict, blk: int) -> models.LaunchpadTrade:
        return models.LaunchpadTrade(
            block_number=blk,
            token=d.get("token", d.get("caller","")).lower(),
            user=d.get("user","").lower(),
            is_buy=bool(d["is_buy"]) if "is_buy" in d else bool(d.get("side", 0)),
            amount_in=int(d["amount_in"]),
            amount_out=int(d["amount_out"]),
        )

    @staticmethod
    def _to_token_created(d: dict, blk: int) -> models.TokenCreated:
        return models.TokenCreated(
            block_number=blk,
            token=d.get("token", d.get("caller","")).lower(),
            creator=d.get("creator","").lower(),
        )


SEQUENCER = Sequencer(_st.State())
