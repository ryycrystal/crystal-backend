from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable

from core import chain as h
from core.ledger.types import (
    AUSD,
    ENTRYPOINT_V06,
    ENTRYPOINT_V07,
    KIND_CONTRACT_UNKNOWN,
    KIND_EOA,
    KIND_EOA_7702,
    KIND_TOKEN,
    KIND_VENUE_CURVE,
    KIND_VENUE_CUSTODY,
    KIND_VENUE_POOL,
    KIND_VENUE_ROUTER,
    KIND_WALLET_4337,
    KIND_ZERO,
    LVMON,
    NATIVE,
    USDC,
    USEROP_EVENT_TOPIC,
    VENUE_KINDS,
    WALLET_KINDS,
    WMON,
    ZERO,
    TxBundle,
)

SOURCE_KNOWN_LIST = "known_list"
SOURCE_GETCODE = "getcode"
SOURCE_HEURISTIC = "heuristic"
SOURCE_USEROP = "userop_event"
USEROP_TAG = "USEROP"
ENTRYPOINTS = frozenset({ENTRYPOINT_V06, ENTRYPOINT_V07})
QUOTE_TOKENS = frozenset({WMON, LVMON, USDC, AUSD})
DELEGATION_PREFIX = "0xef0100"
DELEGATION_CODE_LEN = 48
DEFAULT_RPC_URL = "https://rpc.monad.xyz"
DEFAULT_MAX_RPS = 20.0
GETCODE_BATCH = 50
INSERT_BATCH = 500
DISCOVERY_MIN_TXS = 2

_urlopen = urllib.request.urlopen

_KIND_UPSERT = """
INSERT INTO address_kinds (address, kind, source, first_seen_block, evidence)
VALUES {values}
ON CONFLICT (address) DO UPDATE SET
    kind = EXCLUDED.kind,
    source = EXCLUDED.source,
    evidence = EXCLUDED.evidence,
    first_seen_block = COALESCE(address_kinds.first_seen_block, EXCLUDED.first_seen_block)
{guard}
"""
_KIND_GUARDS = {
    SOURCE_KNOWN_LIST: "",
    SOURCE_GETCODE: None,
    SOURCE_HEURISTIC: "WHERE address_kinds.source = 'getcode'",
    SOURCE_USEROP: "WHERE address_kinds.source = 'getcode'",
}
_VENUE_UPSERT = """
INSERT INTO venues (address, kind, token0, token1, discovered, evidence)
VALUES {values}
ON CONFLICT (address) DO UPDATE SET
    kind = CASE WHEN venues.discovered OR NOT EXCLUDED.discovered THEN EXCLUDED.kind ELSE venues.kind END,
    token0 = COALESCE(EXCLUDED.token0, venues.token0),
    token1 = COALESCE(EXCLUDED.token1, venues.token1),
    discovered = venues.discovered AND EXCLUDED.discovered,
    evidence = COALESCE(EXCLUDED.evidence, venues.evidence)
"""
_KNOWN_QUERIES = {
    "launchpad_pools": "SELECT pool, token_addr, native_addr, token_is_0 FROM launchpad_pools",
    "univ4_pools": "SELECT pool_id, token_addr, native_addr, token_is_0 FROM univ4_pools",
    "crystal_markets": "SELECT market, base_address, quote_address, created_block FROM crystal_markets",
    "crystal_pools": "SELECT market FROM crystal_pools",
    "crystal_vaults": "SELECT vault, base, quote, deployed_block FROM crystal_vaults",
    "launchpad_tokens": "SELECT token, created_block FROM launchpad_tokens",
    "nadfun_v2_tokens": "SELECT token FROM nadfun_v2_tokens",
    "venues": "SELECT address, kind FROM venues",
}


class JsonRpc:
    def __init__(self, url: str, max_rps: float | None = None, attempts: int = 5, timeout: float = 30.0):
        self.url = url
        env_rps = os.getenv("RPC_MAX_RPS")
        self.min_interval = 1.0 / float(max_rps or env_rps or DEFAULT_MAX_RPS)
        self.attempts = attempts
        self.timeout = timeout
        self._last_call = float("-inf")

    def _gate(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def batch(self, calls: list[tuple[str, list]]) -> list:
        if not calls:
            return []
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        body = json.dumps(payload).encode()
        delay = 0.5
        last_error: object = None
        for _ in range(self.attempts):
            self._gate()
            try:
                request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
                with _urlopen(request, timeout=self.timeout) as response:
                    replies = json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
                time.sleep(delay)
                delay *= 2
                continue
            if isinstance(replies, dict):
                replies = [replies]
            by_id = {r.get("id"): r for r in replies if isinstance(r, dict)}
            results = []
            for i in range(len(calls)):
                reply = by_id.get(i)
                if reply is None or "result" not in reply:
                    last_error = reply.get("error") if reply else "missing reply"
                    break
                results.append(reply["result"])
            if len(results) == len(calls):
                return results
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"rpc batch failed after {self.attempts} attempts: {str(last_error)[:200]}")


def known_address_kinds() -> dict[str, str]:
    kinds = {ZERO: KIND_ZERO}
    for addr in QUOTE_TOKENS:
        kinds[addr] = KIND_TOKEN
    for addr in h.NADFUN_ADDRS:
        kinds[addr] = KIND_VENUE_CURVE
    for addr in h.PASSTHROUGH_ADDRS:
        kinds[addr] = KIND_VENUE_ROUTER
    for addr in h.VAULT_FACTORY_ADDRS:
        kinds[addr] = KIND_VENUE_ROUTER
    for addr in ENTRYPOINTS:
        kinds[addr] = KIND_VENUE_ROUTER
    kinds[h.UNIV4_POOL_MANAGER_ADDR] = KIND_VENUE_POOL
    kinds[h.CRYSTAL_ADDR] = KIND_VENUE_CUSTODY
    return kinds


def classify_code(code: str | None) -> str:
    code = (code or "0x").lower()
    if code in ("", "0x"):
        return KIND_EOA
    if code.startswith(DELEGATION_PREFIX) and len(code) == DELEGATION_CODE_LEN:
        return KIND_EOA_7702
    return KIND_CONTRACT_UNKNOWN


def _topic_addr(topic) -> str:
    text = str(topic or "")
    if text.startswith("0x"):
        text = text[2:]
    if len(text) < 40:
        return ""
    return ("0x" + text[-40:]).lower()


def parse_userop_event(addr: str, topics: list[str], data_no0x: str) -> dict | None:
    if len(topics) < 4 or str(topics[0]).lower() != USEROP_EVENT_TOPIC:
        return None
    if (addr or "").lower() not in ENTRYPOINTS:
        return None
    sender = _topic_addr(topics[2])
    if not sender:
        return None
    words = [data_no0x[i : i + 64] for i in range(0, len(data_no0x or ""), 64)]

    def word(i: int) -> int:
        return int(words[i], 16) if i < len(words) and len(words[i]) == 64 else 0

    return {
        "user_op_hash": str(topics[1]).lower(),
        "sender": sender,
        "paymaster": _topic_addr(topics[3]),
        "nonce": word(0),
        "success": word(1) != 0,
        "actual_gas_cost": word(2),
        "actual_gas_used": word(3),
    }


def userop_senders_from_logs(logs: Iterable[dict]) -> list[str]:
    found: list[str] = []
    for log in logs or []:
        topics = log.get("topics") or []
        data = log.get("data") or ""
        parsed = parse_userop_event(log.get("address") or "", topics, data[2:] if data.startswith("0x") else data)
        if parsed and parsed["sender"] not in found:
            found.append(parsed["sender"])
    return found


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class AddressKinds:
    def __init__(
        self,
        cur_factory,
        rpc_url: str | None = None,
        rpc: Callable[[list[tuple[str, list]]], list] | None = None,
        min_txs: int = DISCOVERY_MIN_TXS,
    ):
        self._cur_factory = cur_factory
        self._rpc = rpc or JsonRpc(rpc_url or os.getenv("RPC_HTTP") or DEFAULT_RPC_URL).batch
        self._kinds: dict[str, str] = known_address_kinds()
        self._origins: set[str] = set()
        self._sightings: dict[str, dict[str, dict]] = {}
        self._min_txs = min_txs

    def is_wallet(self, kind: str) -> bool:
        return kind in WALLET_KINDS

    def resolver(self, cur) -> Callable[[str], str]:
        return lambda addr: self.kind(addr, cur)

    def kind(self, addr: str, cur, block: int | None = None) -> str:
        addr = (addr or "").lower()
        if not addr:
            return KIND_ZERO
        return self.kinds_for([addr], cur, block)[addr]

    def kinds_for(self, addrs: Iterable[str], cur, block: int | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        missing: list[str] = []
        for raw in addrs:
            addr = (raw or "").lower()
            if not addr:
                continue
            cached = self._kinds.get(addr)
            if cached:
                out[addr] = cached
            elif addr not in missing:
                missing.append(addr)
        if missing:
            cur.execute("SELECT address, kind FROM address_kinds WHERE address = ANY(%s)", (missing,))
            for addr, kind in cur.fetchall():
                self._kinds[addr] = kind
                out[addr] = kind
            missing = [a for a in missing if a not in out]
        for chunk in _chunks(missing, GETCODE_BATCH):
            codes = self._rpc([("eth_getCode", [addr, "latest"]) for addr in chunk])
            rows = []
            for addr, code in zip(chunk, codes):
                kind = classify_code(code)
                self._kinds[addr] = kind
                out[addr] = kind
                code_text = (code or "0x").lower()
                rows.append((addr, kind, SOURCE_GETCODE, block, {"code_len": max(len(code_text) - 2, 0) // 2}))
            self._put_kinds(cur, rows)
        return out

    def load_known(self, cur) -> None:
        kinds: list[tuple] = []
        venues: list[tuple] = []
        for addr, kind in known_address_kinds().items():
            kinds.append((addr, kind, SOURCE_KNOWN_LIST, None, {"list": "constants"}))
            if kind in VENUE_KINDS:
                venues.append((addr, kind, None, None, False, {"list": "constants"}))

        cur.execute(_KNOWN_QUERIES["launchpad_pools"])
        for pool, token_addr, native_addr, token_is_0 in cur.fetchall():
            token0, token1 = (token_addr, native_addr) if token_is_0 else (native_addr, token_addr)
            kinds.append((pool.lower(), KIND_VENUE_POOL, SOURCE_KNOWN_LIST, None, {"list": "launchpad_pools"}))
            venues.append(
                (pool.lower(), KIND_VENUE_POOL, token0.lower(), token1.lower(), False, {"list": "launchpad_pools"})
            )

        cur.execute(_KNOWN_QUERIES["univ4_pools"])
        for pool_id, token_addr, native_addr, token_is_0 in cur.fetchall():
            token0, token1 = (token_addr, native_addr) if token_is_0 else (native_addr, token_addr)
            venues.append(
                (pool_id.lower(), KIND_VENUE_POOL, token0.lower(), token1.lower(), False, {"list": "univ4_pools"})
            )

        market_tokens: set[str] = set()
        cur.execute(_KNOWN_QUERIES["crystal_markets"])
        seen_markets: set[str] = set()
        for market, base, quote, created_block in cur.fetchall():
            market = market.lower()
            seen_markets.add(market)
            market_tokens.update({base.lower(), quote.lower()})
            kinds.append((market, KIND_VENUE_POOL, SOURCE_KNOWN_LIST, created_block, {"list": "crystal_markets"}))
            venues.append((market, KIND_VENUE_POOL, base.lower(), quote.lower(), False, {"list": "crystal_markets"}))

        cur.execute(_KNOWN_QUERIES["crystal_pools"])
        for (market,) in cur.fetchall():
            market = market.lower()
            if market in seen_markets:
                continue
            kinds.append((market, KIND_VENUE_POOL, SOURCE_KNOWN_LIST, None, {"list": "crystal_pools"}))
            venues.append((market, KIND_VENUE_POOL, None, None, False, {"list": "crystal_pools"}))

        cur.execute(_KNOWN_QUERIES["crystal_vaults"])
        for vault, base, quote, deployed_block in cur.fetchall():
            vault = vault.lower()
            kinds.append((vault, KIND_VENUE_CUSTODY, SOURCE_KNOWN_LIST, deployed_block, {"list": "crystal_vaults"}))
            venues.append((vault, KIND_VENUE_CUSTODY, base.lower(), quote.lower(), False, {"list": "crystal_vaults"}))

        cur.execute(_KNOWN_QUERIES["launchpad_tokens"])
        for token, created_block in cur.fetchall():
            kinds.append((token.lower(), KIND_TOKEN, SOURCE_KNOWN_LIST, created_block, {"list": "launchpad_tokens"}))

        cur.execute(_KNOWN_QUERIES["nadfun_v2_tokens"])
        for (token,) in cur.fetchall():
            kinds.append((token.lower(), KIND_TOKEN, SOURCE_KNOWN_LIST, None, {"list": "nadfun_v2_tokens"}))

        for token in sorted(market_tokens - {ZERO}):
            kinds.append((token, KIND_TOKEN, SOURCE_KNOWN_LIST, None, {"list": "crystal_markets"}))

        deduped: dict[str, tuple] = {}
        for row in kinds:
            deduped.setdefault(row[0], row)
        rows = list(deduped.values())
        self._put_kinds(cur, rows)
        self._put_venues(cur, venues)
        for addr, kind, _source, _block, _evidence in rows:
            self._kinds[addr] = kind

        cur.execute(_KNOWN_QUERIES["venues"])
        for addr, kind in cur.fetchall():
            self._kinds.setdefault(addr.lower(), kind)

    def userop_sender(self, bundle: TxBundle) -> str | None:
        senders = self.userop_senders(bundle)
        return senders[0] if senders else None

    def userop_senders(self, bundle: TxBundle) -> list[str]:
        found: list[str] = []
        entrypoint_of: dict[str, str] = {}
        prefilled = (getattr(bundle, "userop_sender", None) or "").lower()
        if prefilled:
            found.append(prefilled)
        for event in getattr(bundle, "venue_events", None) or []:
            if event.tag != USEROP_TAG or (event.address or "").lower() not in ENTRYPOINTS:
                continue
            sender = (event.parsed.get("sender") or "").lower()
            if sender and sender not in found:
                found.append(sender)
            if sender:
                entrypoint_of.setdefault(sender, event.address.lower())
        for sender in userop_senders_from_logs(getattr(bundle, "logs", None) or []):
            if sender not in found:
                found.append(sender)
        block = getattr(bundle, "block_number", None)
        for sender in found:
            self._mark_wallet_4337(sender, block, entrypoint_of.get(sender))
        return found

    def observe_tx(self, bundle: TxBundle, registry) -> list[str]:
        meta = getattr(bundle, "meta", None)
        if meta is None:
            return []
        origin = (meta.from_addr or "").lower()
        target = (meta.to_addr or "").lower()
        if origin:
            self._origins.add(origin)
            self._sightings.pop(origin, None)
        for sender in self.userop_senders(bundle):
            self._origins.add(sender)
            self._sightings.pop(sender, None)

        token_in: dict[str, set[str]] = {}
        token_out: dict[str, set[str]] = {}
        quote_in: dict[str, set[str]] = {}
        quote_out: dict[str, set[str]] = {}

        def note(table: dict[str, set[str]], addr: str, asset: str) -> None:
            if addr and addr != ZERO:
                table.setdefault(addr, set()).add(asset)

        for leg in bundle.transfers:
            token = (leg.token or "").lower()
            sender = (leg.from_addr or "").lower()
            receiver = (leg.to_addr or "").lower()
            if token in registry:
                note(token_out, sender, token)
                note(token_in, receiver, token)
            elif token in QUOTE_TOKENS:
                note(quote_out, sender, token)
                note(quote_in, receiver, token)
        if meta.value and meta.value > 0:
            note(quote_out, origin, NATIVE)
            note(quote_in, target, NATIVE)
        trace = getattr(bundle, "trace", None)
        if trace is not None and trace.available:
            for sender, receiver, value in trace.transfers:
                if value > 0:
                    note(quote_out, (sender or "").lower(), NATIVE)
                    note(quote_in, (receiver or "").lower(), NATIVE)

        candidates = []
        for addr in set(token_in) | set(token_out):
            if addr in (origin, target) or addr in self._origins:
                continue
            pool_shape = (
                (addr in token_in and addr in token_out)
                or (addr in token_in and addr in quote_out)
                or (addr in token_out and addr in quote_in)
            )
            if pool_shape:
                candidates.append(addr)
        if not candidates:
            return []

        kinds = self._resolve_kinds(candidates, bundle.block_number)
        newly: list[str] = []
        promote: list[tuple[str, dict]] = []
        for addr in sorted(candidates):
            if kinds.get(addr) != KIND_CONTRACT_UNKNOWN:
                continue
            sightings = self._sightings.setdefault(addr, {})
            sightings[bundle.txhash] = {
                "block": bundle.block_number,
                "tokens": sorted(token_in.get(addr, set()) | token_out.get(addr, set())),
                "quotes": sorted(quote_in.get(addr, set()) | quote_out.get(addr, set())),
            }
            if len(sightings) >= self._min_txs:
                promote.append((addr, sightings))
        if not promote:
            return []

        kind_rows = []
        venue_rows = []
        for addr, sightings in promote:
            tokens = sorted({t for s in sightings.values() for t in s["tokens"]})
            quotes = sorted({q for s in sightings.values() for q in s["quotes"]})
            evidence = {
                "rule": "pool_shape_across_txs",
                "txs": sorted(sightings),
                "tokens": tokens,
                "quotes": quotes,
            }
            first_block = min(s["block"] for s in sightings.values())
            token0 = tokens[0] if tokens else None
            token1 = quotes[0] if quotes else (tokens[1] if len(tokens) > 1 else None)
            kind_rows.append((addr, KIND_VENUE_POOL, SOURCE_HEURISTIC, first_block, evidence))
            venue_rows.append((addr, KIND_VENUE_POOL, token0, token1, True, evidence))
            self._kinds[addr] = KIND_VENUE_POOL
            self._sightings.pop(addr, None)
            newly.append(addr)
        with self._cur_factory() as cur:
            self._put_kinds(cur, kind_rows)
            self._put_venues(cur, venue_rows)
        return newly

    def _resolve_kinds(self, addrs: list[str], block: int | None) -> dict[str, str]:
        cached = {a: self._kinds[a] for a in addrs if a in self._kinds}
        missing = [a for a in addrs if a not in cached]
        if not missing or self._cur_factory is None:
            return cached
        with self._cur_factory() as cur:
            cached.update(self.kinds_for(missing, cur, block))
        return cached

    def _mark_wallet_4337(self, sender: str, block: int | None, entrypoint: str | None) -> None:
        current = self._kinds.get(sender)
        if current == KIND_WALLET_4337 or (current is not None and current not in (KIND_CONTRACT_UNKNOWN, KIND_EOA)):
            return
        self._kinds[sender] = KIND_WALLET_4337
        if self._cur_factory is None:
            return
        evidence = {"event": "UserOperationEvent", "entrypoint": entrypoint}
        with self._cur_factory() as cur:
            self._put_kinds(cur, [(sender, KIND_WALLET_4337, SOURCE_USEROP, block, evidence)])

    def _put_kinds(self, cur, rows: list[tuple]) -> None:
        by_source: dict[str, list[tuple]] = {}
        for row in rows:
            by_source.setdefault(row[2], []).append(row)
        for source, source_rows in by_source.items():
            guard = _KIND_GUARDS.get(source, "")
            for chunk in _chunks(source_rows, INSERT_BATCH):
                values = ", ".join(["(%s, %s, %s, %s, %s::jsonb)"] * len(chunk))
                params: list = []
                for addr, kind, src, block, evidence in chunk:
                    params.extend([addr, kind, src, block, json.dumps(evidence) if evidence is not None else None])
                if guard is None:
                    sql = (
                        "INSERT INTO address_kinds (address, kind, source, first_seen_block, evidence) "
                        f"VALUES {values} ON CONFLICT (address) DO NOTHING"
                    )
                else:
                    sql = _KIND_UPSERT.format(values=values, guard=guard)
                cur.execute(sql, params)

    def _put_venues(self, cur, rows: list[tuple]) -> None:
        for chunk in _chunks(rows, INSERT_BATCH):
            values = ", ".join(["(%s, %s, %s, %s, %s, %s::jsonb)"] * len(chunk))
            params: list = []
            for addr, kind, token0, token1, discovered, evidence in chunk:
                params.extend(
                    [
                        addr,
                        kind,
                        token0,
                        token1,
                        bool(discovered),
                        json.dumps(evidence) if evidence is not None else None,
                    ]
                )
            cur.execute(_VENUE_UPSERT.format(values=values), params)
