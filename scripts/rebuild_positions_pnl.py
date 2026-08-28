import argparse
import io
import time

import psycopg2.errors

import core.storage as storage

CHUNK_TRADES = 100_000


def _fold(trades: list[tuple]) -> tuple[list[tuple[str, int, int]], int, int]:
    open_tokens = 0
    basis = 0
    realized_total = 0
    per_trade: list[tuple[str, int, int]] = []
    for txhash, log_index, is_buy, native_amount, token_amount in trades:
        na = int(native_amount)
        ta = int(token_amount)
        if is_buy:
            open_tokens += ta
            basis += na
            realized = 0
        else:
            if open_tokens <= 0 or basis <= 0:
                released = 0
                open_tokens = max(open_tokens - ta, 0)
            elif ta >= open_tokens:
                released = basis
                open_tokens = 0
                basis = 0
            else:
                released = basis * ta // open_tokens
                open_tokens -= ta
                basis -= released
            realized = na - released
            realized_total += realized
        per_trade.append((txhash, int(log_index), realized))
    return per_trade, realized_total, basis


def main() -> None:
    parser = argparse.ArgumentParser(description="recompute realized pnl and cost basis by replaying trades")
    parser.add_argument("--apply", action="store_true", help="write the results, otherwise report only")
    parser.add_argument("--user", default=None, help="restrict to one wallet, for verification")
    args = parser.parse_args()

    storage.init_pool()
    started = time.monotonic()

    where = "WHERE user_address = %s" if args.user else ""
    params = ((args.user or "").lower(),) if args.user else ()

    with storage.db_cursor() as anchor:
        read = anchor.connection.cursor(name="pnl_replay")
        read.itersize = 50_000
        read.execute(
            f"""
            SELECT user_address, token, txhash, log_index, is_buy, native_amount, token_amount
            FROM launchpad_trades
            {where}
            ORDER BY user_address, token, timestamp, log_index
            """,
            params,
        )

        trade_rows: list[tuple[str, int, int]] = []
        position_rows: list[tuple[str, str, int, int]] = []
        current: tuple[str, str] | None = None
        pending: list[tuple] = []
        seen_trades = 0
        positions = 0
        changed_positions = 0

        def flush_position() -> None:
            nonlocal pending, positions, changed_positions
            if current is None or not pending:
                return
            per_trade, realized_total, basis = _fold(pending)
            trade_rows.extend(per_trade)
            position_rows.append((current[0], current[1], realized_total, basis))
            positions += 1
            pending = []

        for row in read:
            user, token, txhash, log_index, is_buy, na, ta = row
            key = (user, token)
            if key != current:
                flush_position()
                current = key
            pending.append((txhash, log_index, is_buy, na, ta))
            seen_trades += 1

            if len(trade_rows) >= CHUNK_TRADES:
                changed_positions += _write(trade_rows, position_rows, args.apply)
                trade_rows, position_rows = [], []
                rate = seen_trades / max(time.monotonic() - started, 0.001)
                print(f"[PNL] {seen_trades:,} trades, {positions:,} positions ({rate:,.0f}/s)", flush=True)

        flush_position()
        changed_positions += _write(trade_rows, position_rows, args.apply)
        read.close()

    verb = "updated" if args.apply else "would update"
    print(
        f"[PNL] complete in {time.monotonic() - started:.1f}s: {seen_trades:,} trades, "
        f"{positions:,} positions, {verb} {changed_positions:,} rows that were wrong",
        flush=True,
    )
    if not args.apply:
        print("[PNL] dry run, pass --apply to write", flush=True)


def _write(trade_rows: list[tuple[str, int, int]], position_rows: list[tuple], apply: bool) -> int:
    if not position_rows:
        return 0
    for attempt in range(5):
        try:
            return _write_once(trade_rows, position_rows, apply)
        except psycopg2.errors.DeadlockDetected:
            if attempt == 4:
                raise
            print(f"[PNL] deadlock, retrying chunk ({attempt + 1}/5)", flush=True)
            time.sleep(1.0 * (attempt + 1))
    return 0


def _write_once(trade_rows: list[tuple[str, int, int]], position_rows: list[tuple], apply: bool) -> int:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _pnl_pos
            (user_address TEXT, token TEXT, realized NUMERIC(50,0), basis NUMERIC(78,0)) ON COMMIT DROP
            """
        )
        cur.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _pnl_tr
            (txhash TEXT, log_index INTEGER, realized NUMERIC(50,0)) ON COMMIT DROP
            """
        )

        buf = io.StringIO()
        for user, token, realized, basis in position_rows:
            buf.write(f"{user}\t{token}\t{realized}\t{basis}\n")
        buf.seek(0)
        cur.copy_from(buf, "_pnl_pos", columns=("user_address", "token", "realized", "basis"))

        if apply:
            buf = io.StringIO()
            for txhash, log_index, realized in trade_rows:
                buf.write(f"{txhash}\t{log_index}\t{realized}\n")
            buf.seek(0)
            cur.copy_from(buf, "_pnl_tr", columns=("txhash", "log_index", "realized"))

        cur.execute(
            """
            SELECT COUNT(*) FROM launchpad_positions p JOIN _pnl_pos n
              ON n.user_address = p.user_address AND n.token = p.token
            WHERE p.realized_pnl_native IS DISTINCT FROM n.realized
               OR p.cost_basis_native IS DISTINCT FROM n.basis
            """
        )
        wrong = int(cur.fetchone()[0] or 0)
        if not apply:
            return wrong

        cur.execute(
            """
            UPDATE launchpad_trades t SET realized_native = n.realized
            FROM _pnl_tr n
            WHERE t.txhash = n.txhash AND t.log_index = n.log_index
              AND t.realized_native IS DISTINCT FROM n.realized
            """
        )
        cur.execute(
            """
            UPDATE launchpad_positions p
            SET realized_pnl_native = n.realized,
                cost_basis_native = n.basis,
                unrealized_pnl_native = crystal_unrealized_pnl(
                    p.balance_token, p.token_bought, p.token_sold, n.basis, k.last_price_native),
                total_pnl_native = n.realized + crystal_unrealized_pnl(
                    p.balance_token, p.token_bought, p.token_sold, n.basis, k.last_price_native)
            FROM _pnl_pos n
            LEFT JOIN launchpad_tokens k ON k.token = n.token
            WHERE p.user_address = n.user_address AND p.token = n.token
            """
        )
    return wrong


if __name__ == "__main__":
    main()
