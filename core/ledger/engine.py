from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

from core import chain as h
from core.ledger.rates import RateBook
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
        self._registry: dict | None = None
        self._market_tokens: dict[str, str] = {}
        self._market_pairs: dict[str, tuple[str, str]] = {}
        self._pools: dict[str, tuple[str, str, bool]] = {}
        self.scope: frozenset[str] | None = None
        self._affected: dict[str, int] = {}
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
        self._pools = self._load_pools(cur)
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
    def _load_pools(cur) -> dict[str, tuple[str, str, bool]]:
        """Which token each pool trades, so a swap that settles internally still names one."""
        out: dict[str, tuple[str, str, bool]] = {}
        cur.execute("SELECT pool_id, token_addr, native_addr, token_is_0 FROM univ4_pools")
        for pool, token, quote, token_is_0 in cur.fetchall():
            if pool and token:
                out[pool.lower()] = ((token or "").lower(), (quote or WMON).lower(), bool(token_is_0))
        cur.execute("SELECT pool, token_addr, native_addr, token_is_0 FROM launchpad_pools")
        for pool, token, quote, token_is_0 in cur.fetchall():
            if pool and token:
                out.setdefault(pool.lower(), ((token or "").lower(), (quote or WMON).lower(), bool(token_is_0)))
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
        """Net this block's transactions into flows.

        A scoped run is the authority for its tokens in every block it covers, so whatever an earlier run
        wrote for them in this block is dropped first and the positions it touched are refolded. Without
        that, the first interpretation of a block would win forever.
        """
        from core.ledger import store
        from core.ledger.netflow import net_transaction

        if self.scope:
            for _, token in store.delete_token_flows(cur, self.scope, blk):
                self._touch(token, blk)
        if not logs:
            return 0
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
                    pools=self._pools,
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
                for token in store.purge_wallets(cur, discovered):
                    self._touch(token, 0)
                flows = [f for f in flows if f.wallet not in discovered]
                self.stats["purged"] += len(discovered)
            tx_flows = net(bundle)
            if self._needs_trace(tx_flows):
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
            self._touch(flow.token, blk)
        return inserted

    def _touch(self, token: str, blk: int) -> None:
        """Remember the earliest block this flush changed for a token; below its watermark forces a refold."""
        current = self._affected.get(token)
        self._affected[token] = int(blk) if current is None else min(current, int(blk))

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
        """A movement nothing visible paid for may have been paid in native MON by an internal call.

        Those leave no log, and without the trace a purchase paid that way is indistinguishable from a
        gift. The trace is fetched for any block: the archive serves call traces all the way back, and it is
        state reads at old blocks that it does not.
        """
        return any(f.basis_state != BASIS_OBSERVED and f.kind in TRACEABLE_KINDS for f in flows)

    def _rates_at(self, blk: int, ts: int, cur) -> Rates:
        if self._rates_fn is None:
            self._rates_fn = RateBook()
        return self._rates_fn(blk, ts, cur)

    def affected_keys(self) -> list[str]:
        return sorted(self._affected)

    def cover(self, cur, tokens, from_block: int, to_block: int) -> None:
        """Commit, alongside the flows, which blocks these tokens are now complete for; None means all."""
        from core.ledger import store

        for token in tokens if tokens is not None else (None,):
            store.extend_coverage(cur, token, from_block, to_block)

    def flush(self, cur) -> int:
        """Fold the touched positions, but only for tokens whose coverage reaches back to their creation.

        A token seen only because another token's transaction moved it has flows that are real evidence
        and a position that would be a fragment of an unreplayed history. Serving the fragment is where the
        leftover tokens' negative balances came from, so it is not served at all.
        """
        if not self._affected:
            return 0
        from core.ledger import fold, store

        touched = self._affected
        self._affected = {}
        covered = store.coverage_from_creation(cur, touched)
        folded = {token: blk for token, blk in touched.items() if token in covered}
        self.stats["uncovered"] += len(touched) - len(folded)
        written = store.refold_tokens(cur, folded, fold.fold_token)
        self.stats["refolded"] += written
        return written
