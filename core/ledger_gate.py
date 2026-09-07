"""The sequencer's only handle on the position ledger.

With the flag off nothing under core.ledger is imported, so a defect there cannot reach the indexer. With
the flag on, the gate refuses to run against a database that lacks the ledger tables instead of raising
inside the block transaction, which would stall indexing on every restart.
"""

from __future__ import annotations

import os


class LedgerGate:
    def __init__(self, cur_factory) -> None:
        self._cur_factory = cur_factory
        self.enabled = os.getenv("LEDGER_ENABLED", "").strip().lower() in {"1", "true"}
        self._engine = None
        self._checked = False

    def _ready(self, cur) -> bool:
        if not self.enabled:
            return False
        if not self._checked:
            self._checked = True
            cur.execute("SELECT to_regclass('wallet_flows'), to_regclass('ledger_token_coverage')")
            row = cur.fetchone()
            if not row or any(value is None for value in row):
                print(
                    "[LEDGER] the ledger tables do not exist here; staying off rather than raising inside the block",
                    flush=True,
                )
                self.enabled = False
                return False
        return True

    def _engine_for(self):
        if self._engine is None:
            from core.ledger.engine import LedgerEngine

            self._engine = LedgerEngine(self._cur_factory, enabled=True)
        return self._engine

    def process_block(self, blk: int, ts: int, logs: list[dict], cur) -> int:
        if not self._ready(cur):
            return 0
        engine = self._engine_for()
        written = engine.process_block(blk, ts, logs, cur)
        engine.cover(cur, None, blk, blk)
        return written

    def flush(self, cur) -> int:
        if not self._ready(cur):
            return 0
        return self._engine_for().flush(cur)
