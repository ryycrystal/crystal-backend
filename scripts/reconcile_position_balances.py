import argparse
import json
import time
import urllib.request

import core.storage as storage
from core.multicall import MULTICALL3_ADDR, decode_multicall3_aggregate3_result, encode_multicall3_aggregate3

PROGRESS_KEY = "reconcile_balances_at"
BALANCE_OF = bytes.fromhex("70a08231")


def rpc_call(url, to, data, block_hex):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, block_hex]}
    ).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read())
    if "result" not in j:
        raise RuntimeError(str(j)[:160])
    return j["result"]


def chain_balances(url, pairs, block_hex):
    """pairs: [(token, wallet)] -> [int | None] in the same order."""
    calls = [(tok, BALANCE_OF + bytes(12) + bytes.fromhex(w[2:])) for tok, w in pairs]
    data = encode_multicall3_aggregate3(calls, allow_failure=True)
    raw = rpc_call(url, MULTICALL3_ADDR, data, block_hex)
    out = []
    for ok, ret in decode_multicall3_aggregate3_result(raw):
        out.append(int.from_bytes(ret[:32], "big") if ok and len(ret) >= 32 else None)
    return out


def main():
    ap = argparse.ArgumentParser(description="point launchpad_positions.balance_token at the real chain balance")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=300)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument("--rpc", default="")
    args = ap.parse_args()

    import os

    url = args.rpc or os.getenv("RPC_HTTP", "https://rpc.monad.xyz")
    storage.init_pool()

    with storage.db_cursor() as cur:
        cur.execute("SELECT MAX(block_number) FROM launchpad_trades")
        head = int(cur.fetchone()[0] or 0)
    # pin every read to the indexer's head so a trade landing mid-sweep cannot make
    # us write a balance newer than the deltas the indexer will apply on top of it
    block_hex = hex(head)
    print(f"[BAL] pinned to block {head}")

    start_at = ""
    if not args.restart:
        start_at = storage.get_meta(PROGRESS_KEY) or ""
        if start_at:
            print(f"[BAL] resuming after {start_at}")

    # NEVER hold one big read: a half-million row fetch sits idle in transaction for
    # minutes, keeps ACCESS SHARE on launchpad_positions, and parks any waiting
    # migration plus every reader behind it. page through with a keyset instead.
    PAGE = 5000

    def next_page(after: str, remaining: int):
        limit = PAGE if not remaining else min(PAGE, remaining)
        with storage.db_cursor() as cur:
            cur.execute(
                """
                SELECT token, user_address, balance_token
                FROM launchpad_positions
                WHERE balance_token > 0 AND (token || ':' || user_address) > %s
                ORDER BY token, user_address
                LIMIT %s
                """,
                (after, limit),
            )
            return cur.fetchall()

    with storage.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM launchpad_positions WHERE balance_token > 0")
        total_est = cur.fetchone()[0]
    print(f"[BAL] ~{total_est:,} positions with a balance; paging {PAGE:,} at a time")

    checked = changed = failed = shown = 0
    zeroed = 0
    t0 = time.perf_counter()
    cursor_key = start_at
    batches_done = 0
    while True:
        remaining = (args.limit - checked) if args.limit else 0
        if args.limit and remaining <= 0:
            break
        page = next_page(cursor_key, remaining)
        if not page:
            break
        cursor_key = f"{page[-1][0]}:{page[-1][1]}"
        for i in range(0, len(page), args.batch):
            chunk = page[i : i + args.batch]
            pairs = [(t, w) for t, w, _ in chunk]
            for attempt in range(4):
                try:
                    got = chain_balances(url, pairs, block_hex)
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"[BAL] batch failed after retries: {str(e)[:100]}")
                        got = [None] * len(pairs)
                    else:
                        time.sleep(2 * (attempt + 1))
            updates = []
            for (tok, w, stored), actual in zip(chunk, got):
                checked += 1
                if actual is None:
                    failed += 1
                    continue
                if int(stored) == actual:
                    continue
                updates.append((w, tok, actual))
                if actual == 0:
                    zeroed += 1
                if shown < args.show:
                    shown += 1
                    print(f"  {w[:12]}.. {tok[:12]}.. {int(stored) / 1e18:,.2f} -> {actual / 1e18:,.2f}")
            if updates and args.apply:
                # short write transaction only, so a waiting migration is never parked
                from psycopg2.extras import execute_values

                with storage.db_cursor() as cur:
                    execute_values(
                        cur,
                        """
                        UPDATE launchpad_positions p SET balance_token = v.bal
                        FROM (VALUES %s) AS v(addr, tok, bal)
                        WHERE p.user_address = v.addr AND p.token = v.tok
                        """,
                        updates,
                        template="(%s, %s, %s::numeric)",
                        page_size=1000,
                    )
                    storage.set_meta(PROGRESS_KEY, f"{chunk[-1][0]}:{chunk[-1][1]}", cur=cur)
            changed += len(updates)
            batches_done += 1
            if batches_done % 20 == 0:
                el = time.perf_counter() - t0
                rate = checked / el if el else 0
                eta = (total_est - checked) / rate / 60 if rate else 0
                print(
                    f"[BAL] {checked:,}/~{total_est:,} changed={changed:,} "
                    f"zeroed={zeroed:,} {rate:.0f}/s eta {eta:.0f}m"
                )

    el = time.perf_counter() - t0
    print(
        f"[BAL] done: checked {checked:,} changed {changed:,} (zeroed {zeroed:,}) failed {failed:,} in {el / 60:.1f}m"
    )


if __name__ == "__main__":
    main()
