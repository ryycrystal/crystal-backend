import os, json, time
from typing import Dict, Tuple, Iterable
from src import Store as _Store

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

try:
    _rust = True
except Exception:
    _rust = False

if _rust:
    _store = _Store(os.environ.get("REDIS_URL"))

    def init(): _store.ping()

    def set_last_indexed_block(blk: int) -> None: _store.set_last_block(blk)
    def get_last_indexed_block() -> int | None: return _store.get_last_block()

    def upsert_vault_meta(vault: str, quote: str, base: str) -> None:
        _store.upsert_vault_meta({vault.lower(): (quote.lower(), base.lower())})

    def load_vault_meta() -> Dict[str, Tuple[str, str]]:
        return _store.load_vault_meta()

    def upsert_vault_bins(vault: str, bucket: int, points: Iterable[tuple[int,int,int,int,int]]) -> None:
        _store.upsert_vault_bins(vault.lower(), int(bucket), list(points))

    def load_vault_bins(vault: str):
        return _store.load_vault_bins(vault.lower())

    def store_snapshot(state) -> None:
        meta = state.vault_meta()
        if meta:
            _store.upsert_vault_meta({v.lower(): (q.lower(), b.lower()) for v,(q,b) in meta.items()})
        for v in meta.keys():
            buckets = state._vault_bins.get(v.lower(), {})
            for bucket, dq in buckets.items():
                if dq:
                    pts = [(p.ts, p.mon_bal, p.quote_bal, p.base_bal, p.total_shares) for p in dq]
                    _store.upsert_vault_bins(v.lower(), int(bucket), pts)

    def load_snapshot(state) -> int | None:
        meta = _store.load_vault_meta()
        for v,(q,b) in meta.items():
            state.register_vault(v, q, b)
        from collections import deque
        from state import _BinPoint
        for v in meta.keys():
            bucket_map = _store.load_vault_bins(v)
            state._vault_bins.setdefault(v.lower(), {})
            for bucket, rows in bucket_map.items():
                dq = state._vault_bins[v.lower()].get(bucket) or deque()
                dq.clear()
                for ts, mon, qbal, bbal, sh in rows:
                    dq.append(_BinPoint(ts=ts, mon_bal=mon, quote_bal=qbal, base_bal=bbal, total_shares=sh))
                state._vault_bins[v.lower()][bucket] = dq
        return get_last_indexed_block()