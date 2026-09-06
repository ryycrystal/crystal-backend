from __future__ import annotations

import json
import os
import time
import urllib.request
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

from core import chain as h
from core.ledger.types import (
    BASIS_OBSERVED,
    ENTRYPOINTS,
    KIND_VENUE_POOL,
    QUOTE_ASSETS,
    USEROP_EVENT_TOPIC,
    WMON,
    ZERO,
    Flow,
    Rates,
    TransferLeg,
    TxBundle,
    VenueEvent,
)

__all__ = ["LedgerEngine", "Rates"]
TRACE_WINDOW_BLOCKS = 500_000
HEAD_TTL_SECONDS = 30.0
RATE_BUCKET_SECONDS = 300
MIN_RATE_SAMPLE_WEI = 10**18
TOKEN_SOURCES = {0: "crystal", 1: "nadfun_v1", 2: "nadfun_v2"}
REGISTERING_TAGS = {"TC", "NFC", "MC"}
TRACEABLE_KINDS = {"buy", "sell", "transfer_in", "transfer_out", "swap_leg"}
CORE_FILL_TAG = "TR"
REFERENCE_SAMPLE = 5
REFERENCE_MIN_TOKENS = 10**15
REFERENCE_MIN_MON_WEI = 10**15
REFERENCE_MIN_MON = Decimal("0.001")
CURVE_TRADE_TAG = "LT"


def _hex_int(raw) -> int:
    if raw is None or raw == "":
        return 0
    try:
        return int(raw, 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return 0


def _topic_addr(topic: str) -> str:
    return "0x" + str(topic).lower().removeprefix("0x")[-40:]


def _log_position(log: dict) -> tuple[int, int]:
    return _hex_int(log.get("transactionIndex")), _hex_int(log.get("logIndex"))


class LedgerEngine:
    def __init__(
        self,
        cur_factory,
        rpc_url=None,
        enabled=None,
        tx_meta_store=None,
        trace_store=None,
        kinds=None,
        rates_fn=None,
        head_fn=None,
    ):
        self._cur_factory = cur_factory
        self._rpc_url = rpc_url or os.getenv("RPC_HTTP") or "https://rpc.monad.xyz"
        if enabled is None:
            enabled = os.getenv("LEDGER_ENABLED", "").strip().lower() in {"1", "true"}
        self.enabled = bool(enabled)
        self._tx_meta = tx_meta_store
        self._traces = trace_store
        self._kinds = kinds
        self._kinds_loaded = False
        self._rates_fn = rates_fn
        self._head_fn = head_fn
        self._registry: dict | None = None
        self._market_tokens: dict[str, str] = {}
        self._market_pairs: dict[str, tuple[str, str]] = {}
        self._affected: set[tuple[str, str]] = set()
        self._head: tuple[int, float] | None = None
        self._rate_cache: dict[int, Rates] = {}
        self._price_cache: dict[tuple[str, int], Decimal | None] = {}
        self.stats: dict[str, int] = defaultdict(int)

    def registry(self, cur) -> dict:
        if self._registry is None:
            self.refresh_registry(cur)
        return self._registry

    def refresh_registry(self, cur) -> dict:
        from core.ledger import store

        self._registry = self._seed_registry(cur, store)
        self._market_tokens = self._load_market_tokens(cur)
        self._market_pairs = self._load_market_pairs(cur)
        return self._registry

    def _seed_registry(self, cur, store) -> dict:
        registry = dict(store.registry(cur, refresh=True))

        def add(token, source, block, quote, decimals=18):
            token = (token or "").lower()
            if not token or token in registry:
                return
            registry[token] = store.register_token(cur, token, source, block, quote, decimals)

        cur.execute("SELECT token, source, created_block, quote_token FROM launchpad_tokens")
        for token, source, block, quote in cur.fetchall():
            add(token, TOKEN_SOURCES.get(int(source or 0), f"source_{int(source or 0)}"), block, (quote or "").lower())
        cur.execute("SELECT token FROM nadfun_v2_tokens")
        for (token,) in cur.fetchall():
            add(token, TOKEN_SOURCES[2], None, WMON)
        cur.execute(
            "SELECT base_address, quote_address, base_decimals, quote_decimals, created_block FROM crystal_markets"
        )
        for base, quote, base_dec, quote_dec, block in cur.fetchall():
            quote = (quote or "").lower()
            add(base, "spot_base", block, quote, int(base_dec or 18))
            if quote and quote not in QUOTE_ASSETS and quote != ZERO:
                add(quote, "spot_quote", block, None, int(quote_dec or 18))
        return registry

    def _register_from_events(self, blk: int, logs: list[dict], cur, store) -> bool:
        markets_changed = False
        for log in logs:
            topics = log.get("topics") or []
            if not topics:
                continue
            tag = h.EVENT_SIGS.get(str(topics[0]).lower())
            if tag not in REGISTERING_TAGS:
                continue
            addr = (log.get("address") or "").lower()
            if not h.accepts_log_for_indexing(tag, addr):
                continue
            if tag == "MC":
                markets_changed = True
                continue
            try:
                parsed = h.PARSERS[tag](addr, topics, str(log.get("data") or "").removeprefix("0x"))
            except Exception:
                continue
            token = ((parsed or {}).get("token") or "").lower()
            if not token or token in self._registry:
                continue
            if tag == "TC":
                source = TOKEN_SOURCES[0]
            elif addr == h.NADFUN_V2_ADDR.lower():
                source = TOKEN_SOURCES[2]
            else:
                source = TOKEN_SOURCES[1]
            quote = (parsed.get("quote_token") or WMON).lower()
            self._registry[token] = store.register_token(cur, token, source, int(blk), quote, 18)
        return markets_changed

    @staticmethod
    def _load_market_tokens(cur) -> dict[str, str]:
        cur.execute("SELECT market, base_address FROM crystal_markets")
        out = {(m or "").lower(): (b or "").lower() for m, b in cur.fetchall() if m and b}
        cur.execute("SELECT market, token FROM launchpad_tokens WHERE market IS NOT NULL")
        for m, t in cur.fetchall():
            if m and t:
                out.setdefault(m.lower(), t.lower())
        return out

    @staticmethod
    def _load_market_pairs(cur) -> dict[str, tuple[str, str]]:
        cur.execute("SELECT market, base_address, quote_address FROM crystal_markets")
        out = {(m or "").lower(): ((b or "").lower(), (q or "").lower()) for m, b, q in cur.fetchall() if m and b}
        cur.execute("SELECT market, token, quote_token FROM launchpad_tokens WHERE market IS NOT NULL")
        for m, t, q in cur.fetchall():
            if m and t:
                out.setdefault(m.lower(), (t.lower(), (q or WMON).lower()))
        return out

    def _kinds_for(self, cur):
        if self._kinds is None:
            from core.ledger.kinds import AddressKinds

            self._kinds = AddressKinds(self._cur_factory, self._rpc_url)
        if not self._kinds_loaded:
            self._kinds.load_known(cur)
            self._kinds_loaded = True
        return self._kinds

    def _tx_meta_for(self):
        if self._tx_meta is None:
            from core.ledger.txmeta import TxMetaStore

            self._tx_meta = TxMetaStore(self._cur_factory, self._rpc_url)
        return self._tx_meta

    def _trace_for(self):
        if self._traces is None:
            from core.ledger.txmeta import TraceStore

            self._traces = TraceStore(self._cur_factory, self._rpc_url)
        return self._traces

    def build_bundles(self, blk: int, ts: int, logs: list[dict], cur) -> tuple[list[TxBundle], set[str]]:
        registry = self.registry(cur)
        groups: dict[str, dict] = {}
        order: list[str] = []
        seen: set[tuple[str, int]] = set()

        for log in sorted(logs, key=_log_position):
            topics = log.get("topics") or []
            txh = (log.get("transactionHash") or "").lower()
            if not topics or not txh:
                continue
            li = _hex_int(log.get("logIndex"))
            if (txh, li) in seen:
                continue
            seen.add((txh, li))

            group = groups.get(txh)
            if group is None:
                group = groups[txh] = {
                    "tx_index": _hex_int(log.get("transactionIndex")),
                    "transfers": [],
                    "events": [],
                    "userop": None,
                    "moved": False,
                }
                order.append(txh)

            addr = (log.get("address") or "").lower()
            topic0 = str(topics[0]).lower()
            if topic0 == USEROP_EVENT_TOPIC:
                if addr in ENTRYPOINTS and len(topics) > 2 and group["userop"] is None:
                    group["userop"] = _topic_addr(topics[2])
                continue

            tag = h.EVENT_SIGS.get(topic0)
            if not tag:
                continue
            data = str(log.get("data") or "")
            try:
                parsed = h.PARSERS[tag](addr, topics, data.removeprefix("0x"))
            except Exception:
                parsed = None
            if parsed is None:
                continue

            if tag == "TF":
                token = (parsed.get("token") or "").lower()
                group["transfers"].append(
                    TransferLeg(
                        log_index=li,
                        token=token,
                        from_addr=(parsed.get("from") or "").lower(),
                        to_addr=(parsed.get("to") or "").lower(),
                        amount=int(parsed.get("amount") or 0),
                    )
                )
                if token in registry:
                    group["moved"] = True
                continue

            if tag == CORE_FILL_TAG:
                token = self._market_tokens.get((parsed.get("market") or "").lower())
                if token:
                    tag = CURVE_TRADE_TAG
                    parsed = {**parsed, "token": token}
            group["events"].append(VenueEvent(tag=tag, log_index=li, parsed=parsed, address=addr))

        bundles = [
            TxBundle(
                txhash=txh,
                block_number=int(blk),
                tx_index=groups[txh]["tx_index"],
                timestamp=int(ts or 0),
                transfers=groups[txh]["transfers"],
                venue_events=groups[txh]["events"],
                meta=None,
                trace=None,
                userop_sender=groups[txh]["userop"],
            )
            for txh in order
        ]
        moved = {txh for txh in order if groups[txh]["moved"]}
        return bundles, moved

    def process_block(self, blk: int, ts: int, logs: list[dict], cur) -> int:
        if not logs:
            return 0
        from core.ledger import store
        from core.ledger.netflow import net_transaction

        if self._registry is None:
            self.refresh_registry(cur)
        if self._register_from_events(blk, logs, cur, store):
            self.refresh_registry(cur)

        bundles, moved = self.build_bundles(blk, ts, logs, cur)
        bundles = [b for b in bundles if b.txhash in moved]
        if not bundles:
            return 0

        registry = self._registry
        kinds = self._kinds_for(cur)

        def kind_of(addr: str) -> str:
            if addr in getattr(kinds, "tx_venues", ()):
                return KIND_VENUE_POOL
            return kinds.kind(addr, cur)

        metas = self._tx_meta_for().get_many([b.txhash for b in bundles])
        self.stats["tx_meta"] += len(metas)
        rates = self._rates_at(blk, ts, cur)

        def reference_price(token: str) -> Decimal | None:
            return self._reference_price(token, blk, ts, cur)

        def net(bundle: TxBundle) -> list[Flow]:
            return list(
                net_transaction(
                    bundle,
                    registry,
                    kind_of,
                    rates=rates,
                    reference_price=reference_price,
                    markets=self._market_pairs,
                )
            )

        flows: list[Flow] = []
        for bundle in bundles:
            bundle = replace(bundle, meta=metas.get(bundle.txhash))
            sender = kinds.userop_sender(bundle, cur) or bundle.userop_sender
            if sender != bundle.userop_sender:
                bundle = replace(bundle, userop_sender=sender)
            discovered = set(kinds.observe_tx(bundle, registry, cur))
            if discovered:
                store.purge_wallets(cur, discovered)
                flows = [f for f in flows if f.wallet not in discovered]
                self._affected = {key for key in self._affected if key[0] not in discovered}
                self.stats["purged"] += len(discovered)
            tx_flows = net(bundle)
            if self._needs_trace(tx_flows) and self._within_trace_window(blk):
                trace = self._trace_for().native_transfers(bundle.txhash)
                self.stats["traces"] += 1
                if trace is not None and trace.available:
                    tx_flows = net(replace(bundle, trace=trace))
            flows.extend(tx_flows)

        self.stats["bundles"] += len(bundles)
        if not flows:
            return 0
        inserted = store.insert_flows(cur, flows)
        self.stats["flows"] += inserted
        for flow in flows:
            self._affected.add((flow.wallet, flow.token))
        return inserted

    def _reference_price(self, token: str, blk: int, ts: int, cur) -> Decimal | None:
        key = (token, int(blk))
        if key in self._price_cache:
            return self._price_cache[key]
        cur.execute(
            """
            SELECT price_native FROM wallet_flows
            WHERE token = %s AND block_number < %s AND price_native IS NOT NULL AND price_native > 0
              AND basis_state = 'observed' AND kind IN ('buy', 'sell')
              AND abs(token_delta) >= %s AND mon_value >= %s
            ORDER BY block_number DESC, tx_index DESC, log_index DESC, sub_index DESC
            LIMIT %s
            """,
            (token, int(blk), REFERENCE_MIN_TOKENS, REFERENCE_MIN_MON, REFERENCE_SAMPLE),
        )
        prices = sorted(Decimal(str(r[0])) for r in cur.fetchall() if r and r[0] is not None)
        price = prices[len(prices) // 2] if prices else None
        if price is None and ts:
            cur.execute(
                """
                SELECT native_amount, token_amount FROM launchpad_trades
                WHERE token = %s AND timestamp <= %s AND token_amount >= %s AND native_amount >= %s
                ORDER BY timestamp DESC, block_number DESC, log_index DESC
                LIMIT 1
                """,
                (token, int(ts), REFERENCE_MIN_TOKENS, REFERENCE_MIN_MON_WEI),
            )
            row = cur.fetchone()
            if row:
                price = Decimal(int(row[0])) / Decimal(int(row[1]))
        if len(self._price_cache) > 4096:
            self._price_cache.clear()
        self._price_cache[key] = price
        return price

    @staticmethod
    def _needs_trace(flows: list[Flow]) -> bool:
        return any(f.basis_state != BASIS_OBSERVED and f.kind in TRACEABLE_KINDS for f in flows)

    def _within_trace_window(self, blk: int) -> bool:
        head = self._head_block()
        return head is not None and head - int(blk) <= TRACE_WINDOW_BLOCKS

    def _head_block(self) -> int | None:
        now = time.monotonic()
        if self._head is not None and now - self._head[1] < HEAD_TTL_SECONDS:
            return self._head[0]
        try:
            head = self._head_fn() if self._head_fn is not None else self._rpc_block_number()
        except Exception:
            head = None
        if head is not None:
            self._head = (int(head), now)
            return int(head)
        return self._head[0] if self._head is not None else None

    def _rpc_block_number(self) -> int:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).encode()
        req = urllib.request.Request(self._rpc_url, data=payload, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(json.loads(resp.read())["result"], 16)

    def _rates_at(self, blk: int, ts: int, cur) -> Rates:
        if self._rates_fn is not None:
            return self._rates_fn(blk, ts, cur)
        bucket = int(ts or 0) // RATE_BUCKET_SECONDS
        cached = self._rate_cache.get(bucket)
        if cached is not None:
            return cached
        mon_usd = self._mon_usd_at(blk, cur)
        lvmon = self._meta_decimal(cur, "lvmon_mon_rate", Decimal(1))
        rates = Rates(mon_usd=mon_usd, lvmon_rate=lvmon, usdc_per_mon=mon_usd)
        if len(self._rate_cache) > 4096:
            self._rate_cache.clear()
        self._rate_cache[bucket] = rates
        return rates

    def _mon_usd_at(self, blk: int, cur) -> Decimal:
        cur.execute(
            """
            SELECT usd_amount / (native_amount / 1e18)
            FROM launchpad_trades
            WHERE block_number <= %s AND native_amount >= %s AND usd_amount > 0
            ORDER BY block_number DESC, log_index DESC
            LIMIT 1
            """,
            (int(blk), MIN_RATE_SAMPLE_WEI),
        )
        row = cur.fetchone()
        if row and row[0]:
            return Decimal(str(row[0]))
        return self._meta_decimal(cur, "mon_price_usd", Decimal(0))

    @staticmethod
    def _meta_decimal(cur, key: str, default: Decimal) -> Decimal:
        cur.execute("SELECT value FROM launchpad_meta WHERE key = %s", (key,))
        row = cur.fetchone()
        if row and row[0] is not None:
            return Decimal(str(row[0]))
        return default

    def affected_keys(self) -> list[tuple[str, str]]:
        return sorted(self._affected)

    def flush(self, cur) -> int:
        if not self._affected:
            return 0
        from core.ledger import fold, store

        keys = sorted(self._affected)
        self._affected.clear()
        written = store.refold(cur, keys, fold.fold)
        self.stats["refolded"] += written
        return written
