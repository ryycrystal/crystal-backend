from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal

import core.storage as storage
import state as _st
from core import chain as h
from core import oracle
from core.storage import db_cursor
from modules import nadfun

ATTRIBUTION_TOLERANCE_BPS = 10

UNIV4_QUOTE_TOKENS = {
    "0x3bd359c1119da7da1d913d1c4d2b7c461115433a",
    "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56",
    "0x754704bc059f8c67012fed69bc8a327a5aafb603",
    "0x00000000efe302beaa2b3e6e1b18d08d69a9012a",
}


class BatchAccumulator:
    def __init__(self):
        self.trades: list[tuple] = []
        self.token_updates: dict[str, dict] = {}
        self.user_updates: dict[str, dict] = {}
        self.position_updates: dict[tuple[str, str], dict] = {}
        self.ohlcv_data: list[tuple] = []
        self.snipers: list[tuple[str, str]] = []

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
        native_reserve=0,
        token_reserve=0,
        realized_native=0,
    ):
        self.trades.append(
            (
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
                int(native_reserve or 0),
                int(token_reserve or 0),
                int(realized_native or 0),
            )
        )

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
        cost_basis_delta: int = 0,
    ):
        if user_address.lower() in h.PASSTHROUGH_ADDRS:
            return

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
                "cost_basis_delta": 0,
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
        p["cost_basis_delta"] += int(cost_basis_delta)
        p["last_price_native"] = last_price_native

    def add_ohlcv(
        self, token: str, resolution_sec: int, bucket_start: int, price_native, native_amount: int, mon_usd=0
    ):
        self.ohlcv_data.append(
            (token.lower(), int(resolution_sec), int(bucket_start), price_native, int(native_amount), mon_usd or 0)
        )

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


class Sequencer:
    def __init__(self, global_state: _st.State) -> None:
        self._state = global_state
        self._logs_by_block: dict[int, list[dict]] = defaultdict(list)
        self._ready_blocks: set[int] = set()
        self._next_block: int | None = None
        self._on_block: Callable[[int], None] | None = None
        self._block_timestamps: dict[int, int] = {}
        self._missing_ts_warned: set[int] = set()
        self.attribution_mismatches = 0

    def set_on_block(self, fn: Callable[[int], None]) -> None:
        self._on_block = fn

    def set_next_block(self, blk: int) -> None:
        self._next_block = blk

    def reset_pending(self, next_block: int | None = None) -> None:
        self._logs_by_block.clear()
        self._ready_blocks.clear()
        self._block_timestamps.clear()
        self._missing_ts_warned.clear()
        self._next_block = next_block

    @staticmethod
    def _log_index(raw_log: dict) -> int:
        li = raw_log.get("logIndex")
        return int(li, 16) if isinstance(li, str) else int(li or 0)

    def add_log(self, raw_log: dict) -> None:
        blk = int(raw_log["blockNumber"], 16) if isinstance(raw_log["blockNumber"], str) else raw_log["blockNumber"]
        if self._next_block is not None and blk < self._next_block:
            return
        if raw_log.get("blockTimestamp") is None and blk in self._block_timestamps:
            raw_log = dict(raw_log)
            raw_log["blockTimestamp"] = hex(self._block_timestamps[blk])
        self._logs_by_block[blk].append(raw_log)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    def note_block(self, blk: int, block_timestamp: int | None = None) -> None:
        if block_timestamp is not None:
            self._block_timestamps[int(blk)] = int(block_timestamp)
        self._ready_blocks.add(blk)
        if self._next_block is None:
            self._next_block = blk
        self._drain()

    def _drain(self) -> None:
        while self._next_block is not None and self._next_block in self._ready_blocks:
            logs = self._logs_by_block.pop(self._next_block, [])
            self._ready_blocks.discard(self._next_block)
            self._process_block(self._next_block, logs)
            self._block_timestamps.pop(self._next_block, None)
            if self._on_block:
                try:
                    self._on_block(self._next_block)
                except Exception as e:
                    print(f"[SQ] Persist Error: {e!r}")
            self._next_block += 1

    def _build_transfer_maps(self, logs: list[dict]) -> dict[tuple[str, str], dict]:
        transfer_maps: dict[tuple[str, str], dict] = {}
        seen_logs: set[tuple[str, int]] = set()

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

            if (txh, log_idx) in seen_logs:
                continue
            seen_logs.add((txh, log_idx))

            key = (txh, token)
            maps = transfer_maps.setdefault(key, {"next": {}, "prev": {}, "ordered": []})
            next_map: dict[str, set[str]] = maps["next"]
            prev_map: dict[str, set[str]] = maps["prev"]

            next_map.setdefault(from_addr, {})[to_addr] = parsed.get("amount") or 0
            prev_map.setdefault(to_addr, {})[from_addr] = parsed.get("amount") or 0

            maps["ordered"].append(
                {
                    "log_idx": log_idx,
                    "from": from_addr,
                    "to": to_addr,
                    "amount": parsed.get("amount") or 0,
                }
            )

        for key, maps in transfer_maps.items():
            maps["ordered"].sort(key=lambda x: x["log_idx"])

        return transfer_maps

    def _verify_attribution(self, blk: int, transfer_maps: dict, cur=None, batch=None) -> None:
        attributed = self._state.take_attributed_token_deltas()
        if not attributed:
            return

        for (txh, token, user), (amount, native_total) in attributed.items():
            maps = transfer_maps.get((txh, token))
            if not maps:
                continue
            net = 0
            deliver_idx = 0
            for t in maps.get("ordered", []):
                if t["to"] == user:
                    net += int(t["amount"] or 0)
                    deliver_idx = max(deliver_idx, int(t["log_idx"] or 0))
                if t["from"] == user:
                    net -= int(t["amount"] or 0)
                    deliver_idx = max(deliver_idx, int(t["log_idx"] or 0))
            if net == 0:
                continue
            diff = amount - net
            if not diff or abs(diff) * 10000 <= abs(net) * ATTRIBUTION_TOLERANCE_BPS:
                continue

            missing = net - amount
            reconciled = False
            if amount != 0 and native_total > 0:
                imputed_native = abs(missing) * native_total // abs(amount)
                reconciled = self._state.apply_reconciliation_trade(
                    token=token,
                    user=user,
                    token_delta=missing,
                    native_amount=imputed_native,
                    blk=blk,
                    ts=int(self._block_timestamps.get(blk, 0)),
                    txh=txh,
                    log_idx=deliver_idx,
                    cur=cur,
                    batch=batch,
                )

            if not reconciled:
                self.attribution_mismatches += 1
                print(
                    f"[SQ] attribution mismatch blk={blk} tx={txh[:12]} token={token[:12]} "
                    f"user={user[:12]} attributed={amount} transfers={net} missing={missing}",
                    flush=True,
                )

    def _register_univ4_from_currencies(self, pool_id, currency0, currency1, cur=None, learned_from="swap"):
        c0 = (currency0 or "").lower()
        c1 = (currency1 or "").lower()
        if not pool_id or (not c0 and not c1):
            return None
        tokens = self._state.launchpad_tokens
        if c0 in tokens and c1 and c1 not in tokens:
            token, quote, token_is_0 = c0, c1, True
        elif c1 in tokens and c0 and c0 not in tokens:
            token, quote, token_is_0 = c1, c0, False
        elif c0 in tokens and not c1:
            token, quote, token_is_0 = c0, "", True
        elif c1 in tokens and not c0:
            token, quote, token_is_0 = c1, "", False
        else:
            return None
        if not quote:
            lp = tokens.get(token)
            quote = (getattr(lp, "quote_token", "") or "").lower()
        if quote not in UNIV4_QUOTE_TOKENS:
            return None
        return self._state.register_univ4_pool(pool_id, token, quote, token_is_0, cur=cur, learned_from=learned_from)

    def _univ4_currencies_from_transfers(self, txh, amount0, amount1, transfer_maps):
        pm = h.UNIV4_POOL_MANAGER_ADDR
        txl = (txh or "").lower()
        found = {}
        for (key_tx, token), maps in transfer_maps.items():
            if key_tx != txl:
                continue
            for t in maps.get("ordered", []):
                if pm not in (t["from"], t["to"]):
                    continue
                amt = int(t["amount"] or 0)
                if amount0 and amt == abs(amount0):
                    found.setdefault(0, token)
                if amount1 and amt == abs(amount1):
                    found.setdefault(1, token)
        return found.get(0, ""), found.get(1, "")

    def _apply_univ4_swap(self, parsed, blk, blk_ts, txh, lii, transfer_maps, cur=None, batch=None):
        pool_id = (parsed.get("pool_id") or "").lower()
        amount0 = int(parsed.get("amount0") or 0)
        amount1 = int(parsed.get("amount1") or 0)
        if not pool_id or amount0 == 0 or amount1 == 0:
            return

        pi = self._state.v4_pools.get(pool_id)
        if pi is None:
            c0, c1 = self._univ4_currencies_from_transfers(txh, amount0, amount1, transfer_maps)
            pi = self._register_univ4_from_currencies(pool_id, c0, c1, cur=cur)
            if pi is None:
                return

        ev = {
            "pool": pool_id,
            "amount0": -amount0,
            "amount1": -amount1,
            "sqrt_price_x96": int(parsed.get("sqrt_price_x96") or 0),
            "user": parsed.get("sender", ""),
        }
        real_user = self._resolve_trade_user(txh, {**ev, "token": pi.token_addr}, pool_id, transfer_maps)
        if real_user:
            ev["user"] = real_user

        self._state.apply_launchpad_trade(ev, blk, blk_ts, txh, lii, h.UNIV4_POOL_MANAGER_ADDR, cur=cur, batch=batch)

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
                print("[DEBUG] No ordered transfers")
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
        skip = {pool, zero_addr, *h.PASSTHROUGH_ADDRS}

        if is_buy:
            net_by_addr = defaultdict(int)
            has_outgoing = set()
            for t in ordered:
                net_by_addr[t["from"]] -= t["amount"]
                net_by_addr[t["to"]] += t["amount"]
                if t["amount"] > 0:
                    has_outgoing.add(t["from"])

            candidates = [(addr, net) for addr, net in net_by_addr.items() if addr not in skip and net > 0]

            if candidates:
                max_net = max(c[1] for c in candidates)
                max_addrs = [addr for addr, net in candidates if net == max_net]

                if len(max_addrs) == 1:
                    if debug:
                        print(f"[DEBUG] Buy: single max retention {max_addrs[0][:10]}... net={max_net}")
                    return max_addrs[0]

                leaf_addrs = [a for a in max_addrs if a not in has_outgoing]
                if len(leaf_addrs) == 1:
                    if debug:
                        print(f"[DEBUG] Buy: single leaf node {leaf_addrs[0][:10]}...")
                    return leaf_addrs[0]
                if leaf_addrs:
                    max_addrs = leaf_addrs

                edges = defaultdict(list)
                for t in ordered:
                    edges[t["from"]].append(t["to"])

                depth = {pool: 0}
                changed = True
                while changed:
                    changed = False
                    for src, dsts in edges.items():
                        if src in depth:
                            for dst in dsts:
                                if dst not in depth:
                                    depth[dst] = depth[src] + 1
                                    changed = True

                max_depth = -1
                deepest = []
                for addr in max_addrs:
                    d = depth.get(addr, 0)
                    if d > max_depth:
                        max_depth = d
                        deepest = [addr]
                    elif d == max_depth:
                        deepest.append(addr)

                if len(deepest) == 1:
                    if debug:
                        print(f"[DEBUG] Buy: deepest node {deepest[0][:10]}... depth={max_depth}")
                    return deepest[0]

                for t in reversed(ordered):
                    if t["to"] in deepest:
                        if debug:
                            print(f"[DEBUG] Buy: last recipient among deepest {t['to'][:10]}...")
                        return t["to"]

                return deepest[0]

            for t in reversed(ordered):
                to_addr = t["to"]
                if to_addr not in skip:
                    if debug:
                        print(f"[DEBUG] Buy: fallback last recipient {to_addr[:10]}...")
                    return to_addr
        elif is_buy is False:
            for t in ordered:
                from_addr = t["from"]
                if from_addr not in skip:
                    if debug:
                        print(f"[DEBUG] Sell: using first sender {from_addr[:10]}...")
                    return from_addr

        if debug:
            print(f"[DEBUG] Could not resolve, returning fallback {fallback_user[:10]}...")

        return fallback_user

    def _classify_pool_sync_kind(self, logs: list[dict], idx: int, txh, parsed_sync: dict | None) -> str | None:
        if parsed_sync is None:
            return None
        market = (parsed_sync.get("market") or "").lower()
        if not market:
            return None
        if idx + 1 >= len(logs):
            return None
        nxt = logs[idx + 1]
        if (nxt.get("transactionHash") or "") != (txh or ""):
            return None
        topics = nxt.get("topics") or []
        if not topics:
            return None
        nxt_tag = h.EVENT_SIGS.get(str(topics[0]).lower())
        if nxt_tag not in {"PMINT", "PBURN"}:
            return None
        try:
            nxt_parsed = h.PARSERS[nxt_tag](
                (nxt.get("address") or "").lower(),
                topics,
                str(nxt.get("data") or "")[2:],
            )
        except Exception:
            return None
        if (nxt_parsed.get("market") or "").lower() != market:
            return None
        return "mint" if nxt_tag == "PMINT" else "burn"

    def _timestamp_for_block_log(self, blk: int, log: dict) -> int:
        raw_ts = log.get("blockTimestamp")
        if raw_ts is None:
            raw_ts = self._block_timestamps.get(blk)
        if isinstance(raw_ts, str):
            return int(raw_ts, 16)
        return int(raw_ts or 0)

    def _preload_missing_v2_tokens(self, blk: int, logs: list[dict], cur) -> None:
        v2_addr = h.NADFUN_V2_ADDR.lower()
        for log in logs:
            if (log.get("address") or "").lower() != v2_addr:
                continue
            topics = log.get("topics") or []
            if len(topics) < 2:
                continue
            if str(topics[0]).lower() not in h.NADFUN_V2_DIRECT_TRADE_TOPICS:
                continue

            token = h._topic_addr(topics[1])
            if not token or token in self._state.launchpad_tokens:
                continue

            try:
                self._state.ensure_v2_launchpad_token(
                    token,
                    blk,
                    self._timestamp_for_block_log(blk, log),
                    cur=cur,
                )
            except Exception as e:
                print(f"[SQ] Failed to discover nad.fun v2 token {token}: {e!r}", flush=True)

    def _process_block(
        self,
        blk: int,
        logs: list[dict],
        cur=None,
        counts_out: dict = None,
        batch: BatchAccumulator = None,
        record_processed: bool = True,
    ):
        logs = sorted(logs, key=self._log_index)
        counts = (
            counts_out
            if counts_out is not None
            else {
                "MC": 0,
                "MPC": 0,
                "TR": 0,
                "PMINT": 0,
                "PBURN": 0,
                "PSYNC": 0,
                "TC": 0,
                "LT": 0,
                "MG": 0,
                "VD": 0,
                "VDP": 0,
                "VWD": 0,
                "VLOCK": 0,
                "VUNLOCK": 0,
                "VCLOSE": 0,
                "VMAX": 0,
                "VLOCKUP": 0,
                "VDECR": 0,
                "NFC": 0,
                "NFB": 0,
                "NFS": 0,
                "NFPEN": 0,
                "NFT": 0,
                "V2SWAP": 0,
                "V2SYNC": 0,
                "TF": 0,
                "V3SWAP": 0,
                "OBU": 0,
                "OBF": 0,
                "IBD": 0,
                "IBW": 0,
                "UR": 0,
            }
        )
        seen = set()

        trade_txs = self._collect_trade_txs(logs)
        has_trades = bool(trade_txs)

        def _mark_processed(c):
            if not record_processed:
                return
            storage.record_block_processed(blk, cur=c)
            bts = self._timestamp_for_block_log(blk, logs[0] if logs else {})
            if bts:
                storage.record_dex_tip(blk, bts, cur=c)
            bh = (logs[0].get("blockHash") or "") if logs else ""
            if bh:
                storage.record_chain_tip(blk, bh, cur=c)
            c.execute("NOTIFY crystal_new_block")

        if cur is None:
            with db_cursor() as cur:
                self._process_block_inner(blk, logs, cur, counts, seen, has_trades, batch, trade_txs)
                _mark_processed(cur)
        else:
            self._process_block_inner(blk, logs, cur, counts, seen, has_trades, batch, trade_txs)
            _mark_processed(cur)

        if counts_out is None:
            print(
                f"[SQ] {blk}: MC {counts['MC']} MPC {counts['MPC']} TR {counts['TR']} "
                f"PMINT {counts['PMINT']} PBURN {counts['PBURN']} PSYNC {counts['PSYNC']} "
                f"TC {counts['TC']} LT {counts['LT']} MG {counts['MG']} "
                f"VD {counts['VD']} VDP {counts['VDP']} VWD {counts['VWD']} "
                f"VLOCK {counts['VLOCK']} VUNLOCK {counts['VUNLOCK']} VCLOSE {counts['VCLOSE']} "
                f"VMAX {counts['VMAX']} VLOCKUP {counts['VLOCKUP']} VDECR {counts['VDECR']} "
                f"V3SWAP {counts['V3SWAP']} V2SWAP {counts['V2SWAP']} "
                f"NFC {counts['NFC']} NFB {counts['NFB']} NFS {counts['NFS']} "
                f"NFPEN {counts['NFPEN']} NFT {counts['NFT']} TF {counts['TF']}"
            )

    def _collect_trade_txs(self, logs: list[dict]) -> set[str]:
        txs: set[str] = set()
        for log in logs:
            topics = log.get("topics") or []
            if not topics:
                continue
            tag = h.EVENT_SIGS.get(topics[0].lower())
            if tag in ("LT", "TR", "NFB", "NFS", "V2SWAP", "V3SWAP", "V4SWAP"):
                txh = (log.get("transactionHash") or "").lower()
                if txh:
                    txs.add(txh)
        return txs

    def _process_block_inner(
        self,
        blk: int,
        logs: list[dict],
        cur,
        counts: dict,
        seen: set,
        has_trades: bool,
        batch: BatchAccumulator = None,
        trade_txs: set[str] | None = None,
    ):
        self._preload_missing_v2_tokens(blk, logs, cur)
        transfer_maps = self._build_transfer_maps(logs) if has_trades else {}
        self._state.take_attributed_token_deltas()

        for idx, log in enumerate(logs):
            blk_ts = self._timestamp_for_block_log(blk, log)
            if blk_ts == 0 and blk not in self._missing_ts_warned:
                self._missing_ts_warned.add(blk)
                print(f"[SQ] Missing blockTimestamp for block {blk}, using 0", flush=True)
            if hasattr(self._state, "note_block_timestamp"):
                try:
                    self._state.note_block_timestamp(blk, blk_ts)
                except Exception:
                    pass
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

            if tag == "MC":
                self._state.apply_market_created(
                    blk, blk_ts, parsed, log.get("address", "").lower(), cur=cur, batch=batch
                )
                if int(parsed.get("marketType") or 0) > 2 and parsed.get("baseAsset"):
                    self._state.apply_migrated(
                        blk,
                        blk_ts,
                        {"token": parsed["baseAsset"]},
                        log.get("address", "").lower(),
                        cur=cur,
                        batch=batch,
                    )

            elif tag == "MPC":
                self._state.apply_market_params_changed(
                    blk, blk_ts, parsed, log.get("address", "").lower(), cur=cur, batch=batch
                )

            elif tag == "TR":
                # the event names whoever called the core, so a routed trade credits
                # the settler or router rather than the trader. every other trade tag
                # already walks the transfer graph back to the real wallet
                real_user = self._resolve_trade_user(
                    txh,
                    parsed,
                    log.get("address", "").lower(),
                    transfer_maps,
                )
                if real_user:
                    parsed = dict(parsed)
                    parsed["user"] = real_user
                self._state.apply_market_trade(
                    blk,
                    blk_ts,
                    parsed,
                    log.get("address", "").lower(),
                    cur=cur,
                    batch=batch,
                    txh=txh,
                    log_idx=lii,
                )

            elif tag == "OBU":
                self._state.apply_orderbook_orders(
                    blk, blk_ts, parsed, log.get("address", "").lower(), txh, lii, cur=cur
                )

            elif tag == "OBF":
                self._state.apply_orderbook_fill(blk, blk_ts, parsed, log.get("address", "").lower(), txh, lii, cur=cur)

            elif tag == "UR":
                self._state.apply_user_registered(blk, blk_ts, parsed, log.get("address", "").lower(), cur=cur)

            elif tag == "REF":
                self._state.apply_referral(blk, blk_ts, parsed, log_idx=lii, cur=cur)

            elif tag in ("IBD", "IBW"):
                storage.insert_crystal_balance_event(
                    txhash=txh or "",
                    log_index=lii,
                    kind=parsed["kind"],
                    user_address=parsed["user"],
                    user_id=parsed["user_id"],
                    token=parsed["token"],
                    amount=parsed["amount"],
                    block_number=blk,
                    timestamp=blk_ts,
                    cur=cur,
                )

            elif tag in ("LPC", "GOV"):
                storage.insert_crystal_protocol_event(
                    txhash=txh or "",
                    log_index=lii,
                    kind=parsed["kind"],
                    params=parsed["params"],
                    block_number=blk,
                    timestamp=blk_ts,
                    cur=cur,
                )

            elif tag == "REFCLAIM":
                storage.write_referral_claims(
                    blk,
                    blk_ts,
                    (txh or "").lower(),
                    lii,
                    parsed["user"],
                    parsed["tokens"],
                    parsed["amounts"],
                    cur=cur,
                )

            elif tag == "PSYNC":
                sync_kind = self._classify_pool_sync_kind(logs, idx, txh, parsed)
                self._state.apply_pool_sync(
                    blk,
                    blk_ts,
                    (txh or "").lower(),
                    parsed,
                    log.get("address", "").lower(),
                    log_idx=lii,
                    sync_kind=sync_kind,
                    cur=cur,
                    batch=batch,
                )

            elif tag in ("PMINT", "PBURN"):
                self._state.apply_pool_liquidity(
                    "mint" if tag == "PMINT" else "burn",
                    blk,
                    blk_ts,
                    parsed,
                    log.get("address", "").lower(),
                    txh,
                    lii,
                    cur=cur,
                )

            elif tag in ("TC", "NFC"):
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

                self._state.apply_launchpad_trade(
                    parsed, blk, blk_ts, txh, lii, log.get("address", "").lower(), cur=cur, batch=batch
                )

            elif tag in ("MG", "NFT"):
                pool = self._state.apply_migrated(blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch)
                if pool:
                    if pool.lower() not in h.ADDRS:
                        h.ADDRS.append(pool.lower())

            elif tag == "VD":
                self._state.apply_vault_deployed(blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch)

            elif tag == "VDP":
                self._state.apply_vault_deposit(
                    blk, blk_ts, txh, parsed, log["address"].lower(), cur=cur, batch=batch, log_idx=lii
                )

            elif tag == "VWD":
                self._state.apply_vault_withdraw(
                    blk, blk_ts, txh, parsed, log["address"].lower(), cur=cur, batch=batch, log_idx=lii
                )

            elif tag == "VLOCK":
                self._state.apply_vault_locked(blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch)

            elif tag == "VUNLOCK":
                self._state.apply_vault_unlocked(blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch)

            elif tag == "VCLOSE":
                self._state.apply_vault_closed(blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch)

            elif tag == "VMAX":
                self._state.apply_vault_max_shares_changed(
                    blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch
                )

            elif tag == "VLOCKUP":
                self._state.apply_vault_lockup_changed(
                    blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch
                )

            elif tag == "VDECR":
                self._state.apply_vault_decrease_on_withdraw_changed(
                    blk, blk_ts, parsed, log["address"].lower(), cur=cur, batch=batch
                )

            elif tag == "TF":
                if parsed is not None:
                    token = (parsed.get("token") or "").lower()
                    mi = self._state.addressToMarket.get(token)
                    is_lp_token = bool(mi is not None and int(getattr(mi, "marketType", 0) or 0) > 1)
                    if (
                        token not in self._state.launchpad_tokens
                        and token not in self._state.token_to_v3_pool
                        and not is_lp_token
                    ):
                        continue
                    if is_lp_token:
                        self._state.apply_pool_transfer(
                            blk,
                            blk_ts,
                            {**parsed, "txhash": (txh or "").lower()},
                            log["address"].lower(),
                            cur=cur,
                            batch=batch,
                        )
                    else:
                        self._state.apply_token_transfer(
                            parsed,
                            blk,
                            blk_ts,
                            log["address"].lower(),
                            cur=cur,
                            batch=batch,
                            in_trade_tx=bool(trade_txs and (txh or "").lower() in trade_txs),
                        )

            elif tag == "V2SYNC":
                pool_addr = (log.get("address") or "").lower()
                sync_r0, sync_r1 = nadfun.peek_pair_sync(pool_addr)
                if sync_r0 or sync_r1:
                    storage.update_pool_reserves(pool_addr, sync_r0, sync_r1, blk, blk_ts, cur=cur)

            elif tag == "V4INIT":
                if parsed:
                    self._register_univ4_from_currencies(
                        parsed.get("pool_id", ""),
                        parsed.get("currency0", ""),
                        parsed.get("currency1", ""),
                        cur=cur,
                        learned_from="initialize",
                    )

            elif tag == "V4SWAP":
                if parsed:
                    self._apply_univ4_swap(parsed, blk, blk_ts, txh, lii, transfer_maps, cur=cur, batch=batch)

            elif tag in ("V2SWAP", "V3SWAP"):
                pool_addr = (log.get("address") or "").lower()
                if tag == "V3SWAP" and pool_addr == oracle.MON_USD_POOL:
                    px = oracle.mon_price_from_v3swap(parsed)
                    if px is not None:
                        self._state.set_mon_price_usd(px)
                elif tag == "V3SWAP" and pool_addr == oracle.LVMON_MON_POOL:
                    rate = oracle.lvmon_rate_from_v3swap(parsed)
                    if rate is not None:
                        self._state.set_lvmon_rate(rate)

                real_user = self._resolve_trade_user(
                    txh,
                    parsed,
                    log.get("address", "").lower(),
                    transfer_maps,
                )
                if real_user:
                    parsed = dict(parsed)
                    parsed["user"] = real_user

                self._state.apply_launchpad_trade(
                    parsed, blk, blk_ts, txh, lii, log.get("address", "").lower(), cur=cur, batch=batch
                )

        self._verify_attribution(blk, transfer_maps, cur=cur, batch=batch)

    def process_chunk(
        self,
        chunk_start: int,
        chunk_end: int,
        logs_by_block: dict[int, list[dict]],
        cur,
    ) -> None:
        counts = {
            "MC": 0,
            "MPC": 0,
            "TR": 0,
            "PMINT": 0,
            "PBURN": 0,
            "PSYNC": 0,
            "TC": 0,
            "LT": 0,
            "MG": 0,
            "VD": 0,
            "VDP": 0,
            "VWD": 0,
            "VLOCK": 0,
            "VUNLOCK": 0,
            "VCLOSE": 0,
            "VMAX": 0,
            "VLOCKUP": 0,
            "VDECR": 0,
            "NFC": 0,
            "NFB": 0,
            "NFS": 0,
            "NFPEN": 0,
            "NFT": 0,
            "TF": 0,
            "V2SWAP": 0,
            "V2SYNC": 0,
            "V3SWAP": 0,
            "OBU": 0,
            "OBF": 0,
            "IBD": 0,
            "IBW": 0,
        }

        batch = BatchAccumulator()
        processed_blocks: list[int] = []
        tip_block = 0
        tip_ts = 0

        for blk in range(chunk_start, chunk_end + 1):
            logs = logs_by_block.get(blk, [])
            bts = self._timestamp_for_block_log(blk, logs[0] if logs else {})
            if bts:
                tip_block, tip_ts = blk, bts
            self._process_block(blk, logs, cur=cur, counts_out=counts, batch=batch, record_processed=False)
            processed_blocks.append(blk)

            self._logs_by_block.pop(blk, None)
            self._ready_blocks.discard(blk)
            self._block_timestamps.pop(blk, None)

        batch.flush(cur)
        # positions are now committed, so the cost basis overlay can safely re-seed
        # from the table again on the next chunk
        self._state.basis_clear_overlay()
        storage.record_blocks_processed_batch(processed_blocks, cur=cur)
        if tip_ts:
            storage.record_dex_tip(tip_block, tip_ts, cur=cur)

        if self._on_block:
            for blk in processed_blocks:
                try:
                    self._on_block(blk)
                except Exception as e:
                    print(f"[SQ] on_block error: {e!r}")

        self._next_block = chunk_end + 1

        print(
            f"[SQ] {chunk_start}-{chunk_end}: MC {counts['MC']} MPC {counts['MPC']} TR {counts['TR']} "
            f"PMINT {counts['PMINT']} PBURN {counts['PBURN']} PSYNC {counts['PSYNC']} "
            f"TC {counts['TC']} LT {counts['LT']} MG {counts['MG']} "
            f"VD {counts['VD']} VDP {counts['VDP']} VWD {counts['VWD']} "
            f"VLOCK {counts['VLOCK']} VUNLOCK {counts['VUNLOCK']} VCLOSE {counts['VCLOSE']} "
            f"VMAX {counts['VMAX']} VLOCKUP {counts['VLOCKUP']} VDECR {counts['VDECR']} "
            f"V3SWAP {counts['V3SWAP']} V2SWAP {counts['V2SWAP']} "
            f"NFC {counts['NFC']} NFB {counts['NFB']} NFS {counts['NFS']} "
            f"NFPEN {counts['NFPEN']} NFT {counts['NFT']} TF {counts['TF']}"
        )


SEQUENCER = Sequencer(_st.State())
