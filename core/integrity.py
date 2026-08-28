import asyncio
import json
import os
import time

import backfill
import core.storage as storage

INTEGRITY_INTERVAL = int(os.getenv("INTEGRITY_INTERVAL", "300"))
INTEGRITY_WINDOW = int(os.getenv("INTEGRITY_WINDOW", "100000"))
INTEGRITY_TIP_MARGIN = int(os.getenv("INTEGRITY_TIP_MARGIN", "256"))
INTEGRITY_STALL_LIMIT = int(os.getenv("INTEGRITY_STALL_LIMIT", "60"))
INTEGRITY_HEAD_LAG_LIMIT = int(os.getenv("INTEGRITY_HEAD_LAG_LIMIT", "100"))


def _processed_state() -> tuple[int | None, int | None, float]:
    with storage.db_cursor() as cur:
        cur.execute(
            "SELECT MIN(number), MAX(number), EXTRACT(EPOCH FROM Now() - MAX(processed_at)) FROM launchpad_blocks"
        )
        row = cur.fetchone()
    p_min = int(row[0]) if row[0] is not None else None
    p_max = int(row[1]) if row[1] is not None else None
    return p_min, p_max, float(row[2] or 0.0)


def _window_counts(start: int, end: int) -> tuple[int, int]:
    with storage.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM launchpad_blocks WHERE number BETWEEN %s AND %s", (start, end))
        processed = int(cur.fetchone()[0])
    holes = storage.count_uncached_processed_blocks(start, end)
    return processed, holes


def evaluate(
    last_block: int,
    stall_secs: float,
    head: int | None,
    window_start: int,
    window_end: int,
    processed: int,
    holes: int,
) -> dict:
    expected = max(window_end - window_start + 1, 0)
    gaps = max(expected - processed, 0) if expected else 0
    lag = max(head - last_block, 0) if head is not None else None
    findings = []
    if stall_secs > INTEGRITY_STALL_LIMIT:
        findings.append(f"no block processed for {int(stall_secs)}s")
    if lag is not None and lag > INTEGRITY_HEAD_LAG_LIMIT:
        findings.append(f"{lag} blocks behind the chain head")
    if gaps:
        findings.append(f"{gaps} blocks never processed in the last {expected}")
    if holes:
        findings.append(f"{holes} processed blocks missing cached logs in the last {expected}")
    return {
        "ts": int(time.time()),
        "last_block": last_block,
        "stall_secs": round(stall_secs, 1),
        "head": head,
        "lag": lag,
        "window": [window_start, window_end],
        "processed_gaps": gaps,
        "cache_holes": holes,
        "findings": findings,
        "ok": not findings,
    }


async def sweep() -> dict:
    p_min, p_max, stall = _processed_state()
    if p_max is None or p_min is None:
        result = {"ts": int(time.time()), "ok": False, "findings": ["nothing indexed yet"]}
        storage.set_meta("integrity_last", json.dumps(result))
        return result
    head = None
    try:
        head = await backfill.get_head_http()
    except Exception as e:
        print(f"[INTEGRITY] head fetch failed {e!r}", flush=True)
    window_end = max(p_max - INTEGRITY_TIP_MARGIN, p_min)
    window_start = max(window_end - INTEGRITY_WINDOW + 1, p_min)
    processed, holes = _window_counts(window_start, window_end)
    result = evaluate(p_max, stall, head, window_start, window_end, processed, holes)
    storage.set_meta("integrity_last", json.dumps(result))
    return result


async def integrity_worker() -> None:
    while True:
        try:
            result = await sweep()
            if result.get("findings"):
                print(f"[INTEGRITY] PROBLEMS: {'; '.join(result['findings'])}", flush=True)
            else:
                print(
                    f"[INTEGRITY] ok: block {result['last_block']}, lag {result.get('lag')}, "
                    f"window {result['window'][0]}-{result['window'][1]} clean",
                    flush=True,
                )
        except Exception as e:
            print(f"[INTEGRITY] sweep failed {e!r}", flush=True)
        await asyncio.sleep(INTEGRITY_INTERVAL)
