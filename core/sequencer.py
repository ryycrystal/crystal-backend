from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Callable, Optional

from core import chain as h
from core import oracle
from core.storage import db_cursor
import core.storage as storage
import state as _st

class BatchAccumulator:
    def __init__(self):
        self.trades: list[tuple] = []
        self.token_updates: dict[str, dict] = {}  # token -> final state
        self.user_updates: dict[str, dict] = {}  # addr -> {native_volume_delta, realized_delta, trade_count_delta}
        self.position_updates: dict[tuple[str, str], dict] = {}  # (user, token) -> deltas
        self.ohlcv_data: list[tuple] = []  # (token, resolution_sec, bucket_start, price_native, native_amount)
        self.snipers: list[tuple[str, str]] = []  # (token, user)

    def add_trade(
        self,
        block_number: int,
        log_index: int,
        timestamp: int,
        token: str,
        user_address: str,
        is_buy: bool,
        native_amount: int,
        token_amount: int,
        usd_amount,
        price_native,
        txhash: str,
    ):
        self.trades.append((
            int(block_number),
            int(log_index),
            int(timestamp),
            token,
            user_address,
            bool(is_buy),
            int(native_amount),
            int(token_amount),
            usd_amount,
            price_native,
            txhash,
        ))

    def set_token_state(self, token: str, state_dict: dict):
        self.token_updates[token.lower()] = state_dict

    def add_user_delta(self, address: str, native_amount: int, realized_delta, trade_count_delta: int = 1):
        addr = address.lower()
        if addr not in self.user_updates:
            self.user_updates[addr] = {
                "native_volume_delta": 0,
                "realized_delta": Decimal(0),
                "trade_count_delta": 0,
            }
        u = self.user_updates[addr]
        u["native_volume_delta"] += int(abs(native_amount))
        u["realized_delta"] += Decimal(realized_delta)
        u["trade_count_delta"] += trade_count_delta

    def add_position_delta(
        self,
        user_address: str,
        token: str,
        token_bought_delta: int,
        token_sold_delta: int,
        native_spent_delta: int,
        native_received_delta: int,
        balance_token_delta: int,
        realized_pnl_delta,
        trade_count_delta: int,
        buy_count_delta: int,
        sell_count_delta: int,
        last_price_native,
    ):
        key = (user_address.lower(), token.lower())
        if key not in self.position_updates:
            self.position_updates[key] = {
                "token_bought_delta": 0,
                "token_sold_delta": 0,
                "native_spent_delta": 0,
                "native_received_delta": 0,
                "balance_token_delta": 0,
                "realized_pnl_delta": Decimal(0),
                "trade_count_delta": 0,
                "buy_count_delta": 0,
                "sell_count_delta": 0,
                "last_price_native": Decimal(0),
            }
        p = self.position_updates[key]
        p["token_bought_delta"] += int(token_bought_delta)
        p["token_sold_delta"] += int(token_sold_delta)
        p["native_spent_delta"] += int(native_spent_delta)
        p["native_received_delta"] += int(native_received_delta)
        p["balance_token_delta"] += int(balance_token_delta)
        p["realized_pnl_delta"] += Decimal(realized_pnl_delta)
        p["trade_count_delta"] += int(trade_count_delta)
        p["buy_count_delta"] += int(buy_count_delta)
        p["sell_count_delta"] += int(sell_count_delta)
        p["last_price_native"] = last_price_native

    def add_ohlcv(self, token: str, resolution_sec: int, bucket_start: int, price_native, native_amount: int):
        self.ohlcv_data.append((token.lower(), int(resolution_sec), int(bucket_start), price_native, int(native_amount)))

    def add_sniper(self, token: str, user: str):
        self.snipers.append((token.lower(), user.lower()))

    def flush(self, cur):
        storage.insert_trades_batch(self.trades, cur)
        storage.update_tokens_batch(self.token_updates, cur)
        storage.update_users_batch(self.user_updates, cur)
        storage.upsert_positions_batch(self.position_updates, cur)
        storage.upsert_ohlcv_batch(self.ohlcv_data, cur)
        storage.add_snipers_batch(self.snipers, cur)

        self.trades.clear()
        self.token_updates.clear()
        self.user_updates.clear()
        self.position_updates.clear()
        self.ohlcv_data.clear()
        self.snipers.clear()

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
    # also tracks ordered list of transfers for simpler first/last lookup
    def _build_transfer_maps(self, logs: list[dict]) -> dict[tuple[str, str], dict]:
        transfer_maps: dict[tuple[str, str], dict] = {}

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
            li = log.get("logIndex")
            log_idx = int(li, 16) if isinstance(li, str) else int(li or 0)

            if not token or not from_addr or not to_addr or not txh:
                continue

            key = (txh, token)
            maps = transfer_maps.setdefault(key, {"next": {}, "prev": {}, "ordered": []})
            next_map: dict[str, set[str]] = maps["next"]
            prev_map: dict[str, set[str]] = maps["prev"]

            next_map.setdefault(from_addr, {})[to_addr] = parsed.get("amount") or 0
            prev_map.setdefault(to_addr, {})[from_addr] = parsed.get("amount") or 0

            maps["ordered"].append({
                "log_idx": log_idx,
                "from": from_addr,
                "to": to_addr,
                "amount": parsed.get("amount") or 0,
            })

        for key, maps in transfer_maps.items():
            maps["ordered"].sort(key=lambda x: x["log_idx"])

        return transfer_maps

    # for a given trade event and transfer chains, find the true user
    def _resolve_trade_user(
        self,
        txh: str,
        parsed: dict,
        pool_addr: str,
        transfer_maps: dict[tuple[str, str], dict],
        debug: bool = False,
    ) -> str:
        pool = (pool_addr or "").lower()
        fallback_user = (parsed.get("user") or "").lower()

        token = (parsed.get("token") or "").lower()
        if not token:
            pi = self._state.v3_pools.get(pool)
            if pi is None or not getattr(pi, "token_addr", None):
                return fallback_user
            token = (pi.token_addr or "").lower()

        key = (txh.lower(), token)
        maps = transfer_maps.get(key)
        if not maps:
            if debug:
                print(f"[DEBUG] No transfer maps for tx={txh[:10]}... token={token[:10]}...")
            return fallback_user

        ordered = maps.get("ordered", [])
        if not ordered:
            if debug:
                print(f"[DEBUG] No ordered transfers")
            return fallback_user

        is_buy = parsed.get("is_buy")

        if is_buy is None:
            pool_sends = any(t["from"] == pool for t in ordered)
            pool_receives = any(t["to"] == pool for t in ordered)
            if pool_sends and not pool_receives:
                is_buy = True
            elif pool_receives and not pool_sends:
                is_buy = False

        if debug:
            print(f"[DEBUG] tx={txh[:10]}... pool={pool[:10]}... is_buy={is_buy}")
            for t in ordered:
                print(f"[DEBUG] [{t['log_idx']}] {t['from'][:10]}... -> {t['to'][:10]}... amt={t['amount']}")

        zero_addr = "0x" + "0" * 40

        if is_buy:
            for t in reversed(ordered):
                to_addr = t["to"]
                if to_addr != pool and to_addr != zero_addr:
                    if debug:
                        print(f"[DEBUG] Buy: using last recipient {to_addr[:10]}...")
                    return to_addr
        elif is_buy is False:
            for t in ordered:
                from_addr = t["from"]
                if from_addr != pool and from_addr != zero_addr:
                    if debug:
                        print(f"[DEBUG] Sell: using first sender {from_addr[:10]}...")
                    return from_addr

        if debug:
            print(f"[DEBUG] Could not resolve, returning fallback {fallback_user[:10]}...")

        return fallback_user

    # actual processing (parsing, route to state handlers, apply changes)
    # if cur is provided, uses that cursor (no commit)
    # if counts_out is provided, accumulates into it instead of printing
    # if batch is provided, accumulates writes instead of executing immediately
    def _process_block(self, blk: int, logs: List[dict], cur=None, counts_out: dict = None, batch: BatchAccumulator = None):
        counts = counts_out if counts_out is not None else {
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

        if cur is None:
            with db_cursor() as cur:
                self._process_block_inner(blk, logs, cur, counts, seen, has_trades, batch)
        else:
            self._process_block_inner(blk, logs, cur, counts, seen, has_trades, batch)

        if counts_out is None:
            print(
                f"[SQ] {blk}: V3SWAP {counts['V3SWAP']} NFC {counts['NFC']} NFB {counts['NFB']} "
                f"NFS {counts['NFS']} NFT {counts['NFT']} TF {counts['TF']} "
            )

    def _process_block_inner(self, blk: int, logs: List[dict], cur, counts: dict, seen: set, has_trades: bool, batch: BatchAccumulator = None):
        transfer_maps = self._build_transfer_maps(logs) if has_trades else {}

        for log in logs:
            blk_ts = int(log.get("blockTimestamp"), 16)
            txh = log.get("transactionHash")
            li = log.get("logIndex")
            lii = int(li, 16) if isinstance(li, str) else int(li or 0)
            uid = (txh, lii)

            if uid in seen:
                continue
            seen.add(uid)

            tag = h.EVENT_SIGS.get(log["topics"][0].lower())
            if not tag:
                continue
            if tag in counts:
                counts[tag] += 1

            parsed = h.PARSERS[tag](log["address"].lower(), log["topics"], log["data"][2:])

            if tag in ("TC", "NFC"):
                self._state.apply_token_created(blk, parsed, blk_ts, log["address"].lower(), cur=cur, batch=batch)

            elif tag in ("LT", "NFB", "NFS"):
                real_user = self._resolve_trade_user(
                    txh,
                    parsed,
                    log.get("address", "").lower(),
                    transfer_maps,
                )
                if real_user:
                    parsed = dict(parsed)
                    parsed["user"] = real_user

                self._state.apply_launchpad_trade(parsed, blk, blk_ts, txh, lii, log.get("address", "").lower(), cur=cur, batch=batch)

            elif tag in ("MG", "NFT"):
                pool = self._state.apply_migrated(blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch)
                if pool:
                    if pool.lower() not in h.ADDRS:
                        h.ADDRS.append(pool.lower())

            elif tag == "TF":
                if parsed is not None:
                    self._state.apply_token_transfer(parsed, blk, blk_ts, log["address"].lower(), cur=cur, batch=batch)

            elif tag == "V3SWAP":
                pool_addr = (log.get("address") or "").lower()
                if pool_addr == "0x659bD0BC4167BA25c62E05656F78043E7eD4a9da".lower():
                    px = oracle.mon_price_from_v3swap(parsed)
                    if px is not None:
                        self._state.set_mon_price_usd(px)

                real_user = self._resolve_trade_user(
                    txh,
                    parsed,
                    log.get("address", "").lower(),
                    transfer_maps,
                )
                if real_user:
                    parsed = dict(parsed)
                    parsed["user"] = real_user

                self._state.apply_launchpad_trade(parsed, blk, blk_ts, txh, lii, log.get("address", "").lower(), cur=cur, batch=batch)

    # batch processes multiple blocks with a shared cursor (one commit for entire batch)
    def process_chunk(
        self,
        chunk_start: int,
        chunk_end: int,
        logs_by_block: dict[int, list[dict]],
        cur,
    ) -> None:
        counts = {"NFC": 0, "NFB": 0, "NFS": 0, "NFT": 0, "TF": 0, "V3SWAP": 0}

        batch = BatchAccumulator()

        for blk in range(chunk_start, chunk_end + 1):
            logs = logs_by_block.get(blk, [])
            self._process_block(blk, logs, cur=cur, counts_out=counts, batch=batch)

            self._logs_by_block.pop(blk, None)
            self._ready_blocks.discard(blk)

            if self._on_block:
                try:
                    self._on_block(blk)
                except Exception as e:
                    print(f"[SQ] on_block error: {e!r}")

        batch.flush(cur)

        self._next_block = chunk_end + 1

        print(
            f"[SQ] {chunk_start}-{chunk_end}: V3SWAP {counts['V3SWAP']} NFC {counts['NFC']} "
            f"NFB {counts['NFB']} NFS {counts['NFS']} NFT {counts['NFT']} TF {counts['TF']}"
        )

SEQUENCER = Sequencer(_st.State())