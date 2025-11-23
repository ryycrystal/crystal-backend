from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Callable, Optional

from core import chain as h
import state as _st

# facilitates the processing of logs into state, in the right order
class Sequencer:
    def __init__(self, global_state: _st.State) -> None:
        self._state = global_state # shared state instance that all logs mutate (global state thats used for all queries n em)
        self._logs_by_block: Dict[int, List[dict]] = defaultdict(list) # pending logs by block number awaiting that block to be marked ready
        self._ready_blocks: set[int] = set() # set of blocks marked ready
        self._next_block: int | None = None # lowest block number we next need to process (or empty if none)
        self._on_block: Optional[Callable[[int], None]] = None # callback invoked after each fully processed block
    
    # callback invoked whenever a block finishes processing
    def set_on_block(self, fn: Callable[[int], None]) -> None:
        self._on_block = fn

    # enqueue a log and start draining (processing) if possible
    def add_log(self, raw_log: dict) -> None:
        blk = int(raw_log["blockNumber"], 16) if isinstance(raw_log["blockNumber"], str) else raw_log["blockNumber"]
        self._logs_by_block[blk].append(raw_log)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    # marks a block as fully seen on-chain so its logs can be processed
    def note_block(self, blk: int) -> None:
        self._ready_blocks.add(blk)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    # processes blocks in order once both logs and ready signal exist
    def _drain(self) -> None:
        while self._next_block is not None and self._next_block in self._ready_blocks:
            logs = self._logs_by_block.pop(self._next_block, [])
            self._ready_blocks.discard(self._next_block)
            self._process_block(self._next_block, logs)
            if self._on_block:
                try:
                    self._on_block(self._next_block)
                except Exception as e:
                    print(f"[SQ] Persist Error: {e!r}")
            self._next_block += 1

    # actual processing (parsing, route to state handlers, apply changes)
    def _process_block(self, blk: int, logs: List[dict]):
        counts = {           
            "NFC": 0, 
            "NFB": 0, 
            "NFS": 0, 
            "NFT": 0,
            "TF": 0,
            "V3SWAP": 0,
        }
        seen = set()

        for log in logs:
            # log metadata
            blk_ts = int(log.get("blockTimestamp"), 16)
            txh = log.get("transactionHash")
            li = log.get("logIndex")
            lii = int(li, 16) if isinstance(li, str) else int(li or 0)
            uid = (txh, lii)
            
            # deduplication cuz for some reason we had duplicates
            if uid in seen:
                continue
            seen.add(uid)

            tag = h.EVENT_SIGS.get(log["topics"][0].lower())
            if not tag:
                continue
            if tag in counts:
                counts[tag] += 1
            
            parsed = h.PARSERS[tag](log["address"].lower(), log["topics"], log["data"][2:])

            if tag in ("TC", "NFC"): # tokencreated/nadfun create
                self._state.apply_token_created(blk, parsed, blk_ts, log["address"].lower())

            elif tag in ("LT", "NFB", "NFS"): # launchpadtrade or nadfun buy/sell
                self._state.apply_launchpad_trade(parsed, blk, blk_ts, txh, log["address"].lower())

            elif tag in ("MG", "NFT"): # migration or nadfun graduation
                pool = self._state.apply_migrated(blk, blk_ts, parsed, log["address"].lower())
                if pool:
                    if pool.lower() not in h.ADDRS:
                        h.ADDRS.append(pool.lower())
                        print(h.ADDRS)
                
            elif tag == "TF": # txfer
                if parsed is not None:
                    self._state.apply_token_transfer(parsed, blk, blk_ts, log["address"].lower())

            elif tag == "V3SWAP": # graduated nadfun v3 pool trade
                self._state.apply_launchpad_trade(parsed, blk, blk_ts, txh, log["address"].lower())
                print("parsed", parsed)

        print(
            f"[SQ] {blk}: V3SWAP {counts['V3SWAP']} NFC {counts['NFC']} NFB {counts['NFB']} "
            f"NFS {counts['NFS']} NFT {counts['NFT']} TF {counts['TF']} "
        )

SEQUENCER = Sequencer(_st.State())