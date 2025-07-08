from __future__ import annotations

"""block‑strict event sequencer

all raw log objects are funnelled through the*single global SEQUENCER

add_log(log): push an individual eth_getLogs / subscription payload
note_block(blk): tell the sequencer that blk has finished producing logs

once all contiguous blocks have been marked ready, the sequencer drains them in
order → parses events → mutates the in‑memory order books (via state.State)

this guarantees no out‑of‑order application even if the rpc/websocket delivers
logs ahead of their headers
"""

from collections import defaultdict
from decimal import Decimal
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
        counts = {"OF": 0, "OU": 0, "UU": 0, "RA": 0}

        for log in logs:
            tag = h.EVENT_SIGS.get(log["topics"][0].lower())
            if tag:
                counts[tag] += 1

            if tag not in ("OF", "OU"):
                continue
            
            parsed = h.PARSERS[tag](log["address"].lower(), log["topics"], log["data"][2:])
            if tag == "OF":
                ev = self._to_order_filled(parsed, blk)
                self._state.apply_order_filled(ev, log["address"].lower())
            else:
                ev = self._to_orders_updated(parsed, blk)
                self._state.apply_orders_updated(ev, log["address"].lower())
        
        print(f"[SQ] {blk}: OF {counts['OF']}  OU {counts['OU']}  UU {counts['UU']}  RA {counts['RA']}")

    @staticmethod
    def _to_order_filled(d: dict, blk: int) -> models.OrderFilled:
        fills = [
            models.Fill(price=f["price"], order_id=f["order_id"], new_quote_size=f["new_size"])  # type: ignore[arg-type]
            for f in d["fills"]
        ]
        return models.OrderFilled(
            block_number=blk,
            caller=d["caller"],
            side=d["side"],
            amount_in=d["amount_in"],
            amount_out=d["amount_out"],
            start_price=d["start_price"],
            end_price=d["end_price"],
            fills=fills,
        )

    @staticmethod
    def _to_orders_updated(d: dict, blk: int) -> models.OrdersUpdated:
        actions = [
            models.OrderAction(
                action=op["action"],
                side=op["side"],
                price=op["price"],
                order_id=op["order_id"],
                quote_size=op["quote_size"],
            )
            for op in d["ops"]
        ]
        return models.OrdersUpdated(block_number=blk, caller=d["caller"], actions=actions)


SEQUENCER = Sequencer(_st.State())
