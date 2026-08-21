import argparse
import time

import core.storage as storage
from core import chain as h
from modules import markets as m

TRADE_TOPIC = "0x9adcf0ad0cda63c4d50f26a48925cf6405df27d422a39c456b5f03f661c82982"
RANGE = 50_000


# replay every cached Trade log into crystal_market_trades. the cache already
# holds the full history, so this touches no rpc and re-runs converge on the
# primary key like every other applier
def main() -> None:
    parser = argparse.ArgumentParser(description="backfill taker trades from the raw log cache")
    parser.add_argument("--start-block", type=lambda x: int(x, 0), default=None)
    args = parser.parse_args()

    storage.init_pool()
    router = h.CONTRACTS["ROUTER"].lower()

    with storage.db_cursor() as cur:
        cur.execute("SELECT market FROM crystal_markets")
        known_markets = {r[0].lower() for r in cur.fetchall()}
        cur.execute("SELECT MIN(number), MAX(number) FROM launchpad_block_logs")
        lo, hi = cur.fetchone()
    if lo is None:
        raise RuntimeError("log cache is empty")
    if not known_markets:
        raise RuntimeError("crystal_markets is empty, run the indexer first")
    lo = max(int(lo), args.start_block or 0)
    hi = int(hi)
    print(f"[TR-BACKFILL] scanning cache {lo}..{hi}", flush=True)

    inserted = 0
    scanned = 0
    unknown_market = 0
    missing_ts = 0
    started = time.monotonic()
    for range_start in range(lo, hi + 1, RANGE):
        range_end = min(range_start + RANGE - 1, hi)
        with storage.db_cursor() as cur:
            cur.execute(
                """
                SELECT number, logs FROM launchpad_block_logs
                WHERE number BETWEEN %s AND %s
                  AND logs @> %s::jsonb
                """,
                (range_start, range_end, f'[{{"topics": ["{TRADE_TOPIC}"]}}]'),
            )
            rows = cur.fetchall()
            for blk, logs in rows:
                for log in logs or []:
                    topics = log.get("topics") or []
                    if not topics or topics[0].lower() != TRADE_TOPIC:
                        continue
                    if (log.get("address") or "").lower() != router:
                        continue
                    parsed = m.parse_trade(router, topics, (log.get("data") or "0x")[2:])
                    if not parsed:
                        continue
                    if (parsed.get("market") or "").lower() not in known_markets:
                        unknown_market += 1
                        continue
                    ts_raw = log.get("blockTimestamp")
                    ts = int(ts_raw, 16) if isinstance(ts_raw, str) else int(ts_raw or 0)
                    if ts <= 0:
                        missing_ts += 1
                        continue
                    li_raw = log.get("logIndex")
                    li = int(li_raw, 16) if isinstance(li_raw, str) else int(li_raw or 0)
                    storage.insert_market_trade(
                        parsed, int(blk), ts, (log.get("transactionHash") or "").lower(), li, cur=cur
                    )
                    inserted += 1
        scanned += range_end - range_start + 1
        if scanned % (RANGE * 10) < RANGE:
            rate = scanned / max(time.monotonic() - started, 0.001)
            eta = (hi - lo - scanned) / max(rate, 0.001)
            print(f"[TR-BACKFILL] {scanned}/{hi - lo + 1} blocks, {inserted} trades ({eta / 60:.0f}m left)", flush=True)

    print(
        f"[TR-BACKFILL] complete: {inserted} taker trades"
        f" ({unknown_market} unknown-market skips, {missing_ts} missing-timestamp skips)",
        flush=True,
    )
    if missing_ts:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
