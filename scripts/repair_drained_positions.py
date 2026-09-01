import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage as storage
from core.storage import db_cursor


def replay_trades(rows: list[tuple]) -> dict:
    bought = sold = spent = received = 0
    open_tokens = basis = 0
    realized = 0
    buys = sells = 0
    for is_buy, native, tok in rows:
        native, tok = int(native), int(tok)
        if is_buy:
            buys += 1
            bought += tok
            spent += native
            open_tokens += tok
            basis += native
        else:
            sells += 1
            sold += tok
            received += native
            if open_tokens <= 0 or basis <= 0:
                released = 0
                open_tokens = max(open_tokens - tok, 0)
            elif tok >= open_tokens:
                released = basis
                open_tokens = 0
                basis = 0
            else:
                released = (basis * tok) // open_tokens
                open_tokens -= tok
                basis -= released
            realized += native - released
    return {
        "token_bought": bought,
        "token_sold": sold,
        "native_spent": spent,
        "native_received": received,
        "realized_pnl_native": realized,
        "cost_basis_native": basis,
        "trade_count": buys + sells,
        "buy_count": buys,
        "sell_count": sells,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="recompute positions whose basis was drained by trade-tx transfer legs, from trade history alone"
    )
    parser.add_argument("--apply", action="store_true", help="write the recomputed rows, default is dry run")
    parser.add_argument("--wallet", default="", help="limit to one wallet")
    parser.add_argument("--token", default="", help="limit to one token")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of rows repaired")
    args = parser.parse_args()

    storage.init_pool()

    where = "p.token_sold > p.token_bought"
    params: list = []
    if args.wallet:
        where += " AND p.user_address = %s"
        params.append(args.wallet.lower())
    if args.token:
        where += " AND p.token = %s"
        params.append(args.token.lower())
    limit = f"LIMIT {int(args.limit)}" if args.limit > 0 else ""

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT p.user_address, p.token, p.token_bought, p.token_sold,
                   p.realized_pnl_native, p.cost_basis_native
            FROM launchpad_positions p
            WHERE {where}
            ORDER BY p.realized_pnl_native DESC
            {limit}
            """,
            params,
        )
        candidates = cur.fetchall()

    print(f"[REPAIR] {len(candidates)} candidate rows (token_sold > token_bought)", flush=True)

    fixed = skipped = 0
    for addr, token, old_bought, old_sold, old_realized, old_basis in candidates:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT is_buy, native_amount, token_amount
                FROM launchpad_trades
                WHERE user_address = %s AND token = %s
                ORDER BY block_number, log_index
                """,
                (addr, token),
            )
            trades = cur.fetchall()

        new = replay_trades(trades)
        if not trades and int(old_bought) == 0 and int(old_realized or 0) == 0:
            skipped += 1
            continue

        print(
            f"[REPAIR] {addr} {token}: bought {int(old_bought)/1e18:,.0f}->{new['token_bought']/1e18:,.0f}"
            f" sold {int(old_sold)/1e18:,.0f}->{new['token_sold']/1e18:,.0f}"
            f" realized {float(old_realized or 0)/1e18:,.1f}->{new['realized_pnl_native']/1e18:,.1f}"
            f" basis {float(old_basis or 0)/1e18:,.1f}->{new['cost_basis_native']/1e18:,.1f}"
            f" ({new['trade_count']} trades)",
            flush=True,
        )

        if args.apply:
            with db_cursor() as cur:
                cur.execute(
                    """
                    UPDATE launchpad_positions
                    SET token_bought = %s, token_sold = %s, native_spent = %s, native_received = %s,
                        realized_pnl_native = %s, cost_basis_native = %s,
                        trade_count = %s, buy_count = %s, sell_count = %s
                    WHERE user_address = %s AND token = %s
                    """,
                    (
                        new["token_bought"],
                        new["token_sold"],
                        new["native_spent"],
                        new["native_received"],
                        new["realized_pnl_native"],
                        new["cost_basis_native"],
                        new["trade_count"],
                        new["buy_count"],
                        new["sell_count"],
                        addr,
                        token,
                    ),
                )
        fixed += 1

    verb = "repaired" if args.apply else "would repair"
    print(f"[REPAIR] {verb} {fixed} rows, skipped {skipped} empty", flush=True)


if __name__ == "__main__":
    main()
