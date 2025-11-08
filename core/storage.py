from __future__ import annotations
import json
import gzip
import os
import time
from typing import Any, Dict, Deque
from collections import deque
from state import _BinPoint

SNAP_PATH = "snapshot.json.gz"
VERSION = 1

def _atomic_write(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _now_ms() -> int:
    return int(time.time() * 1000)

def _to_serializable_state(state) -> Dict[str, Any]:
    meta = state.vault_meta()
    bins_out: Dict[str, Dict[str, list]] = {}

    vault_bins = getattr(state, "_vault_bins", {})
    for v, bucket_map in vault_bins.items():
        v_lower = v.lower()
        v_obj: Dict[str, list] = {}
        for bucket, dq in bucket_map.items():
            series = []
            for p in dq:
                series.append([
                    int(p.ts),
                    str(int(p.mon_bal)),
                    str(int(p.quote_bal)),
                    str(int(p.base_bal)),
                    str(int(p.total_shares)),
                ])
            v_obj[str(int(bucket))] = series
        bins_out[v_lower] = v_obj

    out = {
        "version": VERSION,
        "saved_at_ms": _now_ms(),
        "meta": {v.lower(): [q, b] for v, (q, b) in meta.items()},
        "bins": bins_out,
        "last_block": getattr(state, "_last_block_cursor", None),
    }
    return out

def _from_serialized_state(state, payload: Dict[str, Any]) -> int | None:
    meta = payload.get("meta", {})
    for v, pair in meta.items():
        q, b = pair
        state.register_vault(v, q, b)


    bins = payload.get("bins", {})
    state._vault_bins = getattr(state, "_vault_bins", {})
    for v, bucket_map in bins.items():
        v_lower = v.lower()
        state._vault_bins.setdefault(v_lower, {})
        for bucket_str, series in bucket_map.items():
            dq: Deque[_BinPoint] = deque()
            for ts, mon, qb, bb, sh in series:
                dq.append(_BinPoint(
                    ts=int(ts),
                    mon_bal=int(mon),
                    quote_bal=int(qb),
                    base_bal=int(bb),
                    total_shares=int(sh),
                ))
            state._vault_bins[v_lower][int(bucket_str)] = dq

    last_block = payload.get("last_block")
    if last_block is not None:
        try:
            last_block = int(last_block)
        except Exception:
            last_block = None
    state._last_block_cursor = last_block
    return last_block

def save(state) -> None:
    payload = _to_serializable_state(state)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    comp = gzip.compress(raw, compresslevel=5)
    _atomic_write(SNAP_PATH, comp)

def load(state) -> int | None:
    if not os.path.exists(SNAP_PATH):
        return None
    try:
        with gzip.open(SNAP_PATH, "rb") as f:
            raw = f.read()
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("version", 0)) != VERSION:
            pass
        return _from_serialized_state(state, payload)
    except Exception as e:
        print(f"[Snapshot][load][error] {e!r}")
        return None

def set_last_indexed_block(state, blk: int) -> None:
    state._last_block_cursor = int(blk)
