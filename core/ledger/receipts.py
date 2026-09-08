from __future__ import annotations

import json

from core import chain as h
from core.ledger.txmeta import BLOCK_FETCH_MIN
from core.ledger.types import TRANSFER_TOPIC

RECEIPT_LOG_TABLE = "ledger_receipt_logs"
RECEIPT_LOG_DDL = f"CREATE TABLE IF NOT EXISTS {RECEIPT_LOG_TABLE} (txhash TEXT PRIMARY KEY, logs JSONB NOT NULL)"
_SELECT_SQL = f"SELECT txhash, logs FROM {RECEIPT_LOG_TABLE} WHERE txhash = ANY(%s)"
_INSERT_SQL = f"INSERT INTO {RECEIPT_LOG_TABLE} (txhash, logs) VALUES {{values}} ON CONFLICT (txhash) DO NOTHING"


def _topic_addr(topic) -> str:
    return "0x" + str(topic or "").lower().removeprefix("0x")[-40:]


def _hex_int(raw) -> int:
    if raw is None or raw == "":
        return 0
    return int(raw, 16) if isinstance(raw, str) else int(raw)


def txs_missing_venue_logs(logs: list[dict], venue: str, tokens: set[str] | None = None) -> list[str]:
    """Transactions whose cached logs cannot explain what moved between a registered token and the venue.

    The cache holds the pool manager's swaps and initializations, never its liquidity changes, so a
    transaction with a cached swap can still hide a deposit: the tokens that crossed the venue are only
    accounted for when a cached swap moved that amount, and a cached liquidity event settles it.
    """
    venue = venue.lower()
    swapped: dict[str, list[int]] = {}
    settled: set[str] = set()
    crossed: dict[str, list[int]] = {}
    for log in logs:
        txhash = (log.get("transactionHash") or "").lower()
        if not txhash:
            continue
        address = (log.get("address") or "").lower()
        topics = log.get("topics") or []
        topic0 = str(topics[0]).lower() if topics else ""
        if address == venue:
            tag = h.EVENT_SIGS.get(topic0)
            if tag == "V4SWAP":
                parsed = h.PARSERS[tag](address, topics, str(log.get("data") or "").removeprefix("0x")) or {}
                swapped.setdefault(txhash, []).extend(
                    abs(int(parsed.get(name) or 0)) for name in ("amount0", "amount1")
                )
            elif tag == "V4MODIFY":
                settled.add(txhash)
            else:
                settled.add(txhash)
            continue
        if tokens is not None and address not in tokens:
            continue
        if len(topics) < 3 or topic0 != TRANSFER_TOPIC:
            continue
        if venue in (_topic_addr(topics[1]), _topic_addr(topics[2])):
            crossed.setdefault(txhash, []).append(_hex_int(log.get("data")))
    need = set()
    for txhash, amounts in crossed.items():
        if txhash in settled:
            continue
        fills = swapped.get(txhash)
        if fills is None:
            need.add(txhash)
            continue
        if any(not any(_close(amount, fill) for fill in fills) for amount in amounts):
            need.add(txhash)
    return sorted(need)


def _close(amount: int, fill: int) -> bool:
    return abs(amount - fill) <= max(1, fill // 100)


class ReceiptLogs:
    def __init__(self, rpc, venue: str | None = None, tokens: set[str] | None = None) -> None:
        self._rpc = rpc
        self._venue = (venue or h.UNIV4_POOL_MANAGER_ADDR).lower()
        self._tokens = {t.lower() for t in tokens} if tokens is not None else None
        self.fetched = 0
        self.added = 0

    def complete(self, logs_by_block: dict[int, list[dict]], cur) -> int:
        wanted = {blk: txs_missing_venue_logs(logs, self._venue, self._tokens) for blk, logs in logs_by_block.items()}
        hashes = sorted({txhash for txhashes in wanted.values() for txhash in txhashes})
        if not hashes:
            return 0
        known = self._load(cur, hashes)
        missing = {blk: [t for t in txhashes if t not in known] for blk, txhashes in wanted.items()}
        missing = {blk: txhashes for blk, txhashes in missing.items() if txhashes}
        if missing:
            fetched = self._fetch(missing)
            self._save(cur, fetched)
            known.update(fetched)
        added = 0
        for blk, txhashes in wanted.items():
            logs = logs_by_block[blk]
            present = {((log.get("transactionHash") or "").lower(), _hex_int(log.get("logIndex"))) for log in logs}
            for txhash in txhashes:
                for log in known.get(txhash, []):
                    key = (txhash, _hex_int(log.get("logIndex")))
                    if key in present:
                        continue
                    present.add(key)
                    logs.append(log)
                    added += 1
        self.added += added
        return added

    def _fetch(self, missing: dict[int, list[str]]) -> dict[str, list[dict]]:
        calls: list[tuple[str, list]] = []
        singles: list[str] = []
        for blk in sorted(missing):
            if len(missing[blk]) >= BLOCK_FETCH_MIN:
                calls.append(("eth_getBlockReceipts", [hex(int(blk))]))
            else:
                singles.extend(missing[blk])
        calls.extend(("eth_getTransactionReceipt", [txhash]) for txhash in singles)
        wanted = {txhash for txhashes in missing.values() for txhash in txhashes}
        out: dict[str, list[dict]] = {}
        for (method, params), reply in zip(calls, self._rpc.batch(calls)):
            result = reply.get("result") if "error" not in reply else None
            receipts = result if method == "eth_getBlockReceipts" else [result]
            for receipt in receipts or []:
                if not isinstance(receipt, dict):
                    continue
                if method == "eth_getTransactionReceipt":
                    txhash = params[0]
                else:
                    txhash = (receipt.get("transactionHash") or "").lower()
                if txhash not in wanted:
                    continue
                out[txhash] = [
                    log for log in receipt.get("logs") or [] if (log.get("address") or "").lower() == self._venue
                ]
        self.fetched += len(out)
        return out

    @staticmethod
    def _load(cur, hashes: list[str]) -> dict[str, list[dict]]:
        cur.execute(_SELECT_SQL, (hashes,))
        return {txhash: (json.loads(logs) if isinstance(logs, str) else logs) for txhash, logs in cur.fetchall()}

    @staticmethod
    def _save(cur, fetched: dict[str, list[dict]]) -> None:
        if not fetched:
            return
        params: list = []
        for txhash, logs in fetched.items():
            params.extend([txhash, json.dumps(logs)])
        cur.execute(_INSERT_SQL.format(values=", ".join(["(%s, %s::jsonb)"] * len(fetched))), params)
