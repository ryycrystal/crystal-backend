import argparse
import time

from psycopg2.extras import execute_values

import core.storage as storage
from core import chain as h

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PROGRESS_KEY = "extract_transfers_at"

SELECT_SQL = """
    SELECT b.number,
           l->>'logIndex',
           l->>'transactionHash',
           lower(l->>'address'),
           l->'topics'->>1,
           l->'topics'->>2,
           l->>'data'
    FROM launchpad_block_logs b
    CROSS JOIN LATERAL jsonb_array_elements(b.logs) l
    WHERE b.number BETWEEN %s AND %s
      AND lower(l->'topics'->>0) = %s
      AND EXISTS (SELECT 1 FROM launchpad_tokens t WHERE t.token = lower(l->>'address'))
      AND NOT EXISTS (
            SELECT 1 FROM transfer_venue_addrs v
            WHERE v.addr = '0x' || right(lower(l->'topics'->>1), 40)
      )
      AND NOT EXISTS (
            SELECT 1 FROM transfer_venue_addrs v
            WHERE v.addr = '0x' || right(lower(l->'topics'->>2), 40)
      )
"""

ZERO = "0x" + "0" * 40


def build_venue_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transfer_venue_addrs (addr TEXT PRIMARY KEY);
        TRUNCATE transfer_venue_addrs;
        """
    )
    cur.execute(
        """
        INSERT INTO transfer_venue_addrs (addr)
        SELECT DISTINCT a FROM (
            SELECT lower(market) AS a FROM launchpad_tokens WHERE COALESCE(market, '') <> ''
            UNION SELECT lower(pool) FROM launchpad_pools WHERE COALESCE(pool, '') <> ''
            UNION SELECT lower(market) FROM crystal_markets WHERE COALESCE(market, '') <> ''
            UNION SELECT unnest(%s::text[])
        ) s WHERE a IS NOT NULL AND a <> ''
        ON CONFLICT (addr) DO NOTHING
        """,
        ([*(a.lower() for a in getattr(h, "ADDRS", [])), ZERO],),
    )
    cur.execute("SELECT COUNT(*) FROM transfer_venue_addrs")
    return cur.fetchone()[0]


def to_addr(topic):
    t = (topic or "").lower()
    if len(t) < 40:
        return ""
    return "0x" + t[-40:]


def to_int(raw):
    s = (raw or "").strip()
    if not s:
        return 0
    try:
        return int(s, 16)
    except ValueError:
        return 0


def rows_for_chunk(cur, start, end):
    cur.execute(SELECT_SQL, (start, end, TRANSFER_TOPIC))
    out = []
    for number, log_index, txhash, token, t_from, t_to, data in cur.fetchall():
        frm, to = to_addr(t_from), to_addr(t_to)
        if not frm or not to:
            continue
        out.append(
            (
                int(number),
                to_int(log_index),
                (txhash or "").lower(),
                token,
                frm,
                to,
                to_int((data or "")[:66]),
            )
        )
    return out


def main():
    ap = argparse.ArgumentParser(description="extract launchpad token transfers from the cached block logs")
    ap.add_argument("--chunk", type=int, default=100_000)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=0)
    ap.add_argument("--restart", action="store_true")
    args = ap.parse_args()

    storage.init_pool()
    with storage.db_cursor() as cur:
        n_venues = build_venue_table(cur)
    print(f"[XFER] excluding {n_venues:,} venue addresses (pools, markets, contracts, zero)")
    lo, hi = storage.get_cached_block_range()
    if lo is None:
        raise RuntimeError("no cached blocks")
    start = args.start or lo
    end = args.end or hi

    if not args.restart:
        done = storage.get_meta(PROGRESS_KEY)
        if done and int(done) >= start:
            start = int(done) + 1
            print(f"[XFER] resuming from block {start:,}")

    total = 0
    t0 = time.perf_counter()
    for chunk_start in range(start, end + 1, args.chunk):
        chunk_end = min(chunk_start + args.chunk - 1, end)
        with storage.db_cursor() as cur:
            rows = rows_for_chunk(cur, chunk_start, chunk_end)
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO launchpad_transfers
                        (block_number, log_index, txhash, token, from_addr, to_addr, amount)
                    VALUES %s
                    ON CONFLICT (block_number, log_index) DO NOTHING
                    """,
                    rows,
                    page_size=2000,
                )
            if rows:
                cur.execute(
                    """
                    DELETE FROM launchpad_transfers x
                    USING launchpad_trades tr
                    WHERE x.block_number BETWEEN %s AND %s
                      AND tr.block_number BETWEEN %s AND %s
                      AND tr.txhash = x.txhash
                      AND tr.token = x.token
                    """,
                    (chunk_start, chunk_end, chunk_start, chunk_end),
                )
            storage.set_meta(PROGRESS_KEY, str(chunk_end), cur=cur)
        total += len(rows)
        elapsed = time.perf_counter() - t0
        pct = (chunk_end - start + 1) / max(1, end - start + 1) * 100
        rate = (chunk_end - start + 1) / elapsed if elapsed > 0 else 0
        eta = (end - chunk_end) / rate / 60 if rate > 0 else 0
        print(
            f"[XFER] {chunk_end:,} ({pct:5.1f}%) rows={total:,} "
            f"{rate:,.0f} blk/s eta={eta:,.0f}m",
            flush=True,
        )
    print(f"[XFER] done, {total:,} transfers in {(time.perf_counter()-t0)/60:,.1f}m")


if __name__ == "__main__":
    main()
