import argparse
import asyncio
import time

import httpx
import psycopg2.errors
from psycopg2.extras import Json, execute_values

import core.storage as storage
from core import chain as h
from modules.orderbook import FILL_TOPIC, ORDERS_UPDATED_TOPIC, parse_fill, parse_orders_updated
from state import RPC_HTTP

WINDOW = 100
WINDOWS_PER_BATCH = 20
DEFAULT_RPS = 8.0


class Rpc:
    def __init__(self, url: str, rps: float, timeout: float = 30.0) -> None:
        self.url = url
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.last_request = 0.0
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_request = time.monotonic()

    async def call(self, method: str, params: list) -> dict:
        await self._pace()
        resp = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data)
        return data

    async def batch(self, calls: list[tuple[str, list]]) -> list[dict]:
        await self._pace()
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        resp = await self.client.post(self.url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError(data)
        by_id = {d.get("id"): d for d in data}
        return [by_id.get(i) or {} for i in range(len(calls))]


def default_start_block() -> int | None:
    with storage.db_cursor() as cur:
        cur.execute("SELECT MIN(number) FROM launchpad_blocks")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _int_field(log: dict, key: str) -> int:
    raw = log.get(key)
    return int(raw, 16) if isinstance(raw, str) else int(raw or 0)


def _parse_group(by_block: dict[int, list[dict]]) -> tuple[list[tuple], list[tuple]]:
    event_rows: list[tuple] = []
    fill_rows: list[tuple] = []
    for blk in sorted(by_block):
        for log in by_block[blk]:
            topics = log.get("topics") or []
            if not topics:
                continue
            topic0 = topics[0].lower()
            ts = _int_field(log, "blockTimestamp")
            txh = (log.get("transactionHash") or "").lower()
            li = _int_field(log, "logIndex")
            data = log.get("data") or "0x"

            if topic0 == ORDERS_UPDATED_TOPIC:
                parsed = parse_orders_updated(log.get("address", ""), topics, data)
                if not parsed:
                    continue
                market = (parsed.get("market") or "").lower()
                user = (parsed.get("user") or "").lower()
                for i, o in enumerate(parsed.get("orders") or []):
                    event_rows.append(
                        (
                            txh, li, i, blk, ts, market, user,
                            int(o["flag"]), bool(o["is_buy"]), o["action"],
                            int(o["price"]), int(o["order_id"]), int(o["size"]),
                        )
                    )
            elif topic0 == FILL_TOPIC:
                parsed = parse_fill(log.get("address", ""), topics, data)
                if not parsed:
                    continue
                fill_rows.append(
                    (
                        txh, li, blk, ts, (parsed.get("market") or "").lower(),
                        (parsed.get("maker") or "").lower(),
                        bool(parsed["maker_is_buy"]), int(parsed["price"]),
                        int(parsed["order_id"]), int(parsed["remaining"]),
                        int(parsed["amount_high"]), int(parsed["amount_out"]),
                    )
                )
    return event_rows, fill_rows


def _apply_group(by_block: dict[int, list[dict]], event_rows: list[tuple], fill_rows: list[tuple]) -> dict[str, int]:
    with storage.db_cursor() as cur:
        blocks = sorted(by_block)
        cur.execute("SELECT number, logs FROM launchpad_block_logs WHERE number IN %s", (tuple(blocks),))
        existing = {int(n): logs or [] for n, logs in cur.fetchall()}

        merge_rows = []
        for blk in blocks:
            seen = {
                ((log.get("transactionHash") or "").lower(), str(log.get("logIndex") or ""))
                for log in existing.get(blk, [])
            }
            fresh = [
                log
                for log in by_block[blk]
                if ((log.get("transactionHash") or "").lower(), str(log.get("logIndex") or "")) not in seen
            ]
            if fresh:
                merge_rows.append((int(blk), Json(fresh)))
        if merge_rows:
            execute_values(
                cur,
                """
                INSERT INTO launchpad_block_logs (number, logs) VALUES %s
                ON CONFLICT (number) DO UPDATE SET logs = launchpad_block_logs.logs || EXCLUDED.logs
                """,
                merge_rows,
            )

        fresh_events = storage.batch_insert_orderbook_events(event_rows, cur)
        fresh_fills = storage.batch_insert_orderbook_fills(fill_rows, cur)

    return {"OBU": len(fresh_events), "OBF": len(fresh_fills)}


async def main() -> None:
    parser = argparse.ArgumentParser(description="sweep historical orderbook logs into the cache and the decoded plane")
    parser.add_argument("--rpc", default=RPC_HTTP)
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS)
    parser.add_argument(
        "--start-block", type=lambda x: int(x, 0), default=None, help="default: earliest indexed block"
    )
    parser.add_argument("--end-block", type=lambda x: int(x, 0), default=None, help="default: chain head")
    args = parser.parse_args()

    storage.init_pool()
    router = h.CONTRACTS["ROUTER"].lower()
    rpc = Rpc(args.rpc, args.rps)
    try:
        head = int((await rpc.call("eth_blockNumber", []))["result"], 16)
        end = args.end_block if args.end_block is not None else head
        start = args.start_block
        if start is None:
            saved = storage.get_meta("ob_sweep_progress")
            floor = default_start_block()
            if saved is not None:
                start = int(saved) + 1
                print(f"[OB-SWEEP] resuming from checkpoint block {start}", flush=True)
            elif floor is not None:
                start = floor
                print(f"[OB-SWEEP] sweeping from earliest indexed block {start}", flush=True)
            else:
                raise RuntimeError("nothing indexed yet, pass --start-block explicitly")

        if start > end:
            print(f"[OB-SWEEP] nothing to do: checkpoint {start} is past end {end}", flush=True)
            return

        total_windows = (end - start) // WINDOW + 1
        print(f"[OB-SWEEP] sweeping {start}..{end} ({total_windows} windows)", flush=True)

        counts = {"OBU": 0, "OBF": 0}
        skipped: list[tuple[int, int]] = []
        done = 0
        started = time.monotonic()
        windows = [(w, min(w + WINDOW - 1, end)) for w in range(start, end + 1, WINDOW)]
        for gi in range(0, len(windows), WINDOWS_PER_BATCH):
            group = windows[gi : gi + WINDOWS_PER_BATCH]
            calls = [
                (
                    "eth_getLogs",
                    [
                        {
                            "fromBlock": hex(f),
                            "toBlock": hex(t),
                            "address": router,
                            "topics": [[ORDERS_UPDATED_TOPIC, FILL_TOPIC]],
                        }
                    ],
                )
                for f, t in group
            ]

            results = None
            for attempt in range(6):
                try:
                    candidate = await rpc.batch(calls)
                    if any("error" in r for r in candidate):
                        bad = next(r["error"] for r in candidate if "error" in r)
                        raise RuntimeError(f"getLogs error: {bad}")
                    results = candidate
                    break
                except Exception as e:
                    print(f"[OB-SWEEP] group at {group[0][0]} attempt {attempt + 1}/6 failed ({e!r})", flush=True)
                    await asyncio.sleep(2.0 * (attempt + 1))
            if results is None:
                skipped.append((group[0][0], group[-1][1]))
                continue

            by_block: dict[int, list[dict]] = {}
            for r in results:
                for log in r.get("result") or []:
                    blk_raw = log.get("blockNumber")
                    blk = int(blk_raw, 16) if isinstance(blk_raw, str) else int(blk_raw or 0)
                    by_block.setdefault(blk, []).append(log)

            if by_block:
                event_rows, fill_rows = _parse_group(by_block)
                for db_attempt in range(5):
                    try:
                        group_counts = _apply_group(by_block, event_rows, fill_rows)
                        counts["OBU"] += group_counts["OBU"]
                        counts["OBF"] += group_counts["OBF"]
                        break
                    except psycopg2.errors.DeadlockDetected:
                        if db_attempt == 4:
                            raise
                        print(f"[OB-SWEEP] deadlock at group {group[0][0]}, re-applying", flush=True)
                        await asyncio.sleep(0.5 * (db_attempt + 1))

            if not skipped:
                storage.set_meta("ob_sweep_progress", str(group[-1][1]))

            done += len(group)
            if done % 2000 < WINDOWS_PER_BATCH:
                rate = done / max(time.monotonic() - started, 0.001)
                eta = (total_windows - done) / max(rate, 0.001)
                print(
                    f"[OB-SWEEP] {done}/{total_windows} windows, OBU {counts['OBU']} OBF {counts['OBF']} "
                    f"({rate:.0f} win/s, eta {eta / 60:.0f}m)",
                    flush=True,
                )
    finally:
        await rpc.close()

    print(f"[OB-SWEEP] complete: OBU {counts['OBU']}, OBF {counts['OBF']}", flush=True)
    print("[OB-SWEEP] run rebuild_orderbook_orders.py to fold the history into order state", flush=True)
    if skipped:
        for f, t in skipped:
            print(f"[OB-SWEEP] SKIPPED {f}..{t} after repeated failures, rerun to retry", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
