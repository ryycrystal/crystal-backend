import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage as storage
from core import chain as h
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
        description="clear the phantom positions a pass through router accumulated from forwarded tokens"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--traders",
        default="",
        help="comma separated router addresses. defaults to the known pass through list in core.chain",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "also drop rows where the router still has trades of its own. only for a known stateless "
            "execution contract, whose forwarded trades could not be traced back to a wallet"
        ),
    )
    args = parser.parse_args()

    storage.init_pool()
    routers = [a.strip().lower() for a in args.traders.split(",") if a.strip()] or list(h.PASSTHROUGH_ADDRS)
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit > 0 else ""

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT user_address, token, balance_token, token_bought, token_sold, realized_pnl_native
            FROM launchpad_positions
            WHERE user_address = ANY(%s)
            ORDER BY realized_pnl_native DESC
            {limit_sql}
            """,
            (routers,),
        )
        rows = cur.fetchall()

    print(f"[ROUTERPOS] {len(rows)} position rows held by {len(routers)} routers", flush=True)

    deleted = rewritten = kept = 0
    for addr, token, balance, bought, sold, realized in rows:
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
        has_own_trades = new["trade_count"] > 0
        holds_tokens = int(balance or 0) > 0

        if not holds_tokens and (not has_own_trades or args.force):
            # nothing of its own left once the forwarded trades were reattributed,
            # so the row is pure noise on holder lists and pnl leaderboards
            residue = f" ({new['trade_count']} untraceable forwarded trades)" if has_own_trades else ""
            print(
                f"[ROUTERPOS] delete {addr[:12]} {token[:12]} "
                f"(was pnl {float(realized or 0) / 1e18:,.1f}){residue}",
                flush=True,
            )
            if args.apply:
                with db_cursor() as cur:
                    cur.execute(
                        "DELETE FROM launchpad_positions WHERE user_address = %s AND token = %s",
                        (addr, token),
                    )
            deleted += 1
            continue

        if has_own_trades:
            print(
                f"[ROUTERPOS] rewrite {addr[:12]} {token[:12]} bought "
                f"{int(bought or 0) / 1e18:,.0f}->{new['token_bought'] / 1e18:,.0f} "
                f"pnl {float(realized or 0) / 1e18:,.1f}->{new['realized_pnl_native'] / 1e18:,.1f}",
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
            rewritten += 1
            continue

        kept += 1

    verb = "applied" if args.apply else "would apply"
    print(f"[ROUTERPOS] {verb}: deleted {deleted}, rewritten {rewritten}, kept {kept}", flush=True)


if __name__ == "__main__":
    main()
