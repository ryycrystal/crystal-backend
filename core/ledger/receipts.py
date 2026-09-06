from __future__ import annotations

import json

from core import chain as h
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


def txs_missing_venue_logs(logs: list[dict], venue: str) -> list[str]:
    venue = venue.lower()
    have: set[str] = set()
    need: set[str] = set()
    for log in logs:
        txhash = (log.get("transactionHash") or "").lower()
        if not txhash:
            continue
        if (log.get("address") or "").lower() == venue:
            have.add(txhash)
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        if venue in (_topic_addr(topics[1]), _topic_addr(topics[2])):
            need.add(txhash)
    return sorted(need - have)


class ReceiptLogs:
    def __init__(self, rpc, venue: str | None = None) -> None:
        self._rpc = rpc
        self._venue = (venue or h.UNIV4_POOL_MANAGER_ADDR).lower()
        self.fetched = 0
        self.added = 0

    def complete(self, logs_by_block: dict[int, list[dict]], cur) -> int:
        wanted = {blk: txs_missing_venue_logs(logs, self._venue) for blk, logs in logs_by_block.items()}
        hashes = sorted({txhash for txhashes in wanted.values() for txhash in txhashes})
        if not hashes:
            return 0
        known = self._load(cur, hashes)
        missing = [txhash for txhash in hashes if txhash not in known]
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

    def _fetch(self, hashes: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        replies = self._rpc.batch([("eth_getTransactionReceipt", [txhash]) for txhash in hashes])
        for txhash, reply in zip(hashes, replies):
            receipt = reply.get("result") if "error" not in reply else None
            if not isinstance(receipt, dict):
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
