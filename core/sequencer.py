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

    # per block, build transfer chains keyed by (tx_hash, token), for each txfer next[from] = to, prev[to] = from
    # so for a given (tx, token) we follow buy is pool -> ... -> user using next, sell is user -> ... -> pool using prev
    def _build_transfer_maps(self, logs: list[dict]) -> dict[tuple[str, str], dict[str, dict[str, set[str]]]]:
        transfer_maps: dict[tuple[str, str], dict[str, dict[str, set[str]]]] = {}

        for log in logs:
            topics = log.get("topics") or []
            if not topics:
                continue

            tag = h.EVENT_SIGS.get(topics[0].lower())
            if tag != "TF":
                continue

            data_no0x = (log.get("data") or "")[2:]
            parsed = h.PARSERS[tag](log.get("address", "").lower(), topics, data_no0x)
            if parsed is None:
                continue

            token = (parsed.get("token") or "").lower()
            from_addr = (parsed.get("from") or "").lower()
            to_addr = (parsed.get("to") or "").lower()
            txh = (log.get("transactionHash") or "").lower()

            if not token or not from_addr or not to_addr or not txh:
                continue

            key = (txh, token)
            maps = transfer_maps.setdefault(key, {"next": {}, "prev": {}})
            next_map: dict[str, set[str]] = maps["next"]
            prev_map: dict[str, set[str]] = maps["prev"]

            next_map.setdefault(from_addr, set()).add(to_addr)
            prev_map.setdefault(to_addr, set()).add(from_addr)

        return transfer_maps

    # for a given trade event and transfer chains, find the true user
    def _resolve_trade_user(
        self,
        txh: str,
        parsed: dict,
        pool_addr: str,
        transfer_maps: dict[tuple[str, str], dict[str, dict[str, set[str]]]],
    ) -> str:
        pool = (pool_addr or "").lower()

        token = (parsed.get("token") or "").lower()
        if not token:
            pi = self._state.v3_pools.get(pool)
            if pi is None or not getattr(pi, "token_addr", None):
                return (parsed.get("user") or "").lower()
            token = (pi.token_addr or "").lower()

        key = (txh.lower(), token)
        maps = transfer_maps.get(key)
        if not maps:
            return (parsed.get("user") or "").lower()

        next_map: dict[str, set[str]] = maps["next"]
        prev_map: dict[str, set[str]] = maps["prev"]

        component: set[str] = set()
        queue: list[str] = [pool]
        max_nodes = 16

        while queue and len(component) < max_nodes:
            addr = queue.pop()
            if addr in component:
                continue
            component.add(addr)

            for nb in next_map.get(addr, ()):
                if nb not in component:
                    queue.append(nb)
            for nb in prev_map.get(addr, ()):
                if nb not in component:
                    queue.append(nb)

        if pool not in component or len(component) == 1:
            return (parsed.get("user") or "").lower()

        in_deg: dict[str, int] = {a: 0 for a in component}
        out_deg: dict[str, int] = {a: 0 for a in component}

        for a in component:
            outs = next_map.get(a, set())
            outs_in_comp = [b for b in outs if b in component]
            out_deg[a] = len(outs_in_comp)
            for b in outs_in_comp:
                in_deg[b] += 1

        is_buy = parsed.get("is_buy")
        if is_buy is None:
            pool_out = out_deg.get(pool, 0)
            pool_in = in_deg.get(pool, 0)
            if pool_out > 0 and pool_in == 0:
                is_buy = True
            elif pool_in > 0 and pool_out == 0:
                is_buy = False

        candidate: str | None = None
        if is_buy:
            for addr in component:
                if out_deg.get(addr, 0) == 0 and in_deg.get(addr, 0) > 0:
                    if candidate is not None and candidate != addr:
                        candidate = None
                        break
                    candidate = addr
        else:
            for addr in component:
                if in_deg.get(addr, 0) == 0 and out_deg.get(addr, 0) > 0:
                    if candidate is not None and candidate != addr:
                        candidate = None
                        break
                    candidate = addr

        if not candidate or candidate == pool:
            return (parsed.get("user") or "").lower()

        return candidate

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

        has_trades = False
        for log in logs:
            topics = log.get("topics") or []
            if not topics:
                continue
            tag = h.EVENT_SIGS.get(topics[0].lower())
            if tag in ("LT", "NFB", "NFS", "V3SWAP"):
                has_trades = True
                break

        transfer_maps = self._build_transfer_maps(logs) if has_trades else {}

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
                real_user = self._resolve_trade_user(
                    txh,
                    parsed,
                    log.get("address", "").lower(),
                    transfer_maps,
                )
                if real_user:
                    parsed = dict(parsed)
                    parsed["user"] = real_user

                self._state.apply_launchpad_trade(parsed, blk, blk_ts, txh, lii, log.get("address", "").lower())

            elif tag in ("MG", "NFT"): # migration or nadfun graduation
                pool = self._state.apply_migrated(blk, blk_ts, parsed, log["address"].lower())
                if pool:
                    if pool.lower() not in h.ADDRS:
                        h.ADDRS.append(pool.lower())
                
            elif tag == "TF": # txfer
                if parsed is not None:
                    self._state.apply_token_transfer(parsed, blk, blk_ts, log["address"].lower())

            elif tag == "V3SWAP": # graduated nadfun v3 pool trade
                real_user = self._resolve_trade_user(
                    txh,
                    parsed,
                    log.get("address", "").lower(),
                    transfer_maps,
                )
                if real_user:
                    parsed = dict(parsed)
                    parsed["user"] = real_user

                self._state.apply_launchpad_trade(parsed, blk, blk_ts, txh, lii, log.get("address", "").lower())

        print(
            f"[SQ] {blk}: V3SWAP {counts['V3SWAP']} NFC {counts['NFC']} NFB {counts['NFB']} "
            f"NFS {counts['NFS']} NFT {counts['NFT']} TF {counts['TF']} "
        )

SEQUENCER = Sequencer(_st.State())