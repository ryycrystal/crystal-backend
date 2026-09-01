import argparse
import time

import psycopg2
from decimal import Decimal

from psycopg2.extras import execute_values

import core.storage as storage
from core import chain as h

ZERO = "0x" + "0" * 40
PROGRESS_KEY = "rebuild_basis_transfers_at"


def venue_addresses(cur):
    addrs = {a.lower() for a in getattr(h, "ADDRS", [])}
    addrs.add(ZERO)
    cur.execute("SELECT DISTINCT lower(market) FROM launchpad_tokens WHERE COALESCE(market, '') <> ''")
    addrs.update(r[0] for r in cur.fetchall())
    cur.execute("SELECT DISTINCT lower(pool) FROM launchpad_pools WHERE COALESCE(pool, '') <> ''")
    addrs.update(r[0] for r in cur.fetchall())
    cur.execute("SELECT DISTINCT lower(market) FROM crystal_markets WHERE COALESCE(market, '') <> ''")
    addrs.update(r[0] for r in cur.fetchall())
    return addrs


def events_for_token(cur, token):
    cur.execute(
        """
        SELECT block_number, log_index, 't', user_address, is_buy, native_amount, token_amount, txhash
        FROM launchpad_trades WHERE token = %s
        UNION ALL
        SELECT block_number, log_index, 'x', from_addr, NULL, 0, amount, to_addr
        FROM launchpad_transfers WHERE token = %s
        ORDER BY 1, 2
        """,
        (token, token),
    )
    return cur.fetchall()


def fold(events, venues):
    users: dict[str, list] = {}
    sell_realized: list[tuple] = []

    def slot(addr):
        return users.setdefault(addr, [0, 0, 0, 0, Decimal(0)])

    for blk, idx, kind, actor, is_buy, native, amount, counterparty in events:
        actor = (actor or "").lower()
        amount = int(amount or 0)
        if amount <= 0:
            continue
        if kind == "t":
            u = slot(actor)
            if is_buy:
                u[0] += amount
                u[1] += int(native or 0)
                u[2] += amount
            else:
                open_t, basis = u[0], u[1]
                if open_t <= 0 or basis <= 0:
                    released = 0
                    u[0] = max(open_t - amount, 0)
                elif amount >= open_t:
                    released = basis
                    u[0] = 0
                    u[1] = 0
                else:
                    released = basis * amount // open_t
                    u[0] = open_t - amount
                    u[1] = basis - released
                u[3] += amount
                u[4] += Decimal(int(native or 0) - released)
                sell_realized.append((counterparty, idx, int(native or 0) - released))
        else:
            to = (counterparty or "").lower()
            if actor in venues or to in venues or actor == to:
                continue
            src = users.get(actor)
            if not src or src[0] <= 0 or src[1] <= 0:
                continue
            move = min(amount, src[0])
            released = src[1] * move // src[0]
            src[0] -= move
            src[1] -= released
            src[2] -= move
            dst = slot(to)
            dst[0] += move
            dst[1] += released
            dst[2] += move
    return users, sell_realized


def main():
    ap = argparse.ArgumentParser(description="rebuild cost basis folding trades and transfers together")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=10)
    ap.add_argument("--restart", action="store_true")
    args = ap.parse_args()

    storage.init_pool()
    with storage.db_cursor() as cur:
        venues = venue_addresses(cur)
        cur.execute("SELECT token FROM launchpad_tokens ORDER BY token")
        tokens = [r[0] for r in cur.fetchall()]
    print(f"[BASIS] {len(tokens):,} tokens, {len(venues):,} venue addresses excluded")

    start_at = ""
    if not args.restart:
        start_at = storage.get_meta(PROGRESS_KEY) or ""
        if start_at:
            tokens = [t for t in tokens if t > start_at]
            print(f"[BASIS] resuming after {start_at}")
    if args.limit:
        tokens = tokens[: args.limit]

    changed = 0
    scanned = 0
    shown = 0
    t0 = time.perf_counter()
    for i, token in enumerate(tokens, 1):
        for attempt in range(4):
            try:
                with storage.db_cursor() as cur:
                    events = events_for_token(cur, token)
                    if not events:
                        break
                    users, sell_realized = fold(events, venues)
                    cur.execute(
                        "SELECT user_address, token_bought, cost_basis_native, realized_pnl_native "
                        "FROM launchpad_positions WHERE token = %s",
                        (token,),
                    )
                    current = {r[0]: (int(r[1] or 0), int(r[2] or 0), Decimal(r[3] or 0)) for r in cur.fetchall()}

                    updates = []
                    for addr, (open_t, basis, bought, sold, realized) in users.items():
                        cur_row = current.get(addr)
                        if cur_row is None:
                            continue
                        if cur_row[0] == bought and cur_row[1] == basis and cur_row[2] == realized:
                            continue
                        updates.append((addr, token, int(bought), int(basis), realized))
                        if shown < args.show:
                            print(
                                f"  {addr[:12]}.. {token[:12]}.. bought {cur_row[0]/1e18:,.2f}->{bought/1e18:,.2f} "
                                f"basis {cur_row[1]/1e18:,.4f}->{basis/1e18:,.4f} "
                                f"realized {float(cur_row[2])/1e18:,.4f}->{float(realized)/1e18:,.4f}"
                            )
                            shown += 1
                    scanned += len(current)
                    changed += len(updates)

                    if updates and args.apply:
                        execute_values(
                            cur,
                            """
                            UPDATE launchpad_positions p SET
                                token_bought = v.bought,
                                cost_basis_native = v.basis,
                                realized_pnl_native = v.realized
                            FROM (VALUES %s) AS v(addr, tok, bought, basis, realized)
                            WHERE p.user_address = v.addr AND p.token = v.tok
                            """,
                            [(a, t, b, c, d) for a, t, b, c, d in updates],
                            template="(%s, %s, %s::numeric, %s::numeric, %s::numeric)",
                            page_size=1000,
                        )
                        cur.execute(
                            """
                            UPDATE launchpad_positions p SET
                                unrealized_pnl_native = crystal_unrealized_pnl(
                                    p.balance_token, p.token_bought, p.token_sold, p.cost_basis_native, k.last_price_native),
                                total_pnl_native = p.realized_pnl_native + crystal_unrealized_pnl(
                                    p.balance_token, p.token_bought, p.token_sold, p.cost_basis_native, k.last_price_native)
                            FROM launchpad_tokens k
                            WHERE k.token = p.token AND p.token = %s
                            """,
                            (token,),
                        )
                    if args.apply and sell_realized:
                        execute_values(
                            cur,
                            """
                            UPDATE launchpad_trades t SET realized_native = v.realized
                            FROM (VALUES %s) AS v(tx, li, realized)
                            WHERE t.txhash = v.tx AND t.log_index = v.li
                              AND t.realized_native IS DISTINCT FROM v.realized
                            """,
                            sell_realized,
                            template="(%s, %s::integer, %s::numeric)",
                            page_size=2000,
                        )
                    if args.apply:
                        storage.set_meta(PROGRESS_KEY, token, cur=cur)

                break
            except psycopg2.errors.DeadlockDetected:
                if attempt == 3:
                    raise
                time.sleep(1 + attempt * 2)
        if i % 500 == 0:
            el = time.perf_counter() - t0
            print(
                f"[BASIS] {i:,}/{len(tokens):,} tokens, {changed:,} rows changed of {scanned:,} "
                f"({i/el:,.1f} tok/s, eta {(len(tokens)-i)/max(i/el,0.001)/60:,.0f}m)",
                flush=True,
            )

    print(f"\n[BASIS] {changed:,} position rows differ out of {scanned:,} scanned")
    if not args.apply:
        print("[BASIS] dry run, nothing written")


if __name__ == "__main__":
    main()
