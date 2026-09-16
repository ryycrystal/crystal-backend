import argparse

import core.storage as storage

WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
SOURCE_V2 = 2

# a v2 token's quote_token is fixed by its own create event, so this names the curve it
# launched on even after it migrates. v1 carries no quote and always launches in mon
SCOPE = "(SELECT token FROM launchpad_tokens WHERE source = %(v2)s AND quote_token <> %(w)s)"

# dated snapshots and audit tables are left alone on purpose: they are the record of a
# past state, and token_decimals is a shared cache that costs nothing to keep.
# launchpad_positions_live and ledger_token_coverage are views over these tables, so
# they empty on their own and cannot be deleted from directly
TABLES = [
    ("launchpad_trades", "token"),
    ("launchpad_positions", "token"),
    ("launchpad_ohlcv", "token"),
    ("launchpad_snipers", "token"),
    ("launchpad_transfers", "token"),
    ("launchpad_pools", "token_addr"),
    ("launchpad_pair_fees", "base_token"),
    ("univ4_pools", "token_addr"),
    ("nadfun_v2_tokens", "token"),
    ("token_registry", "token"),
    ("positions_v2", "token"),
    ("wallet_flows", "token"),
    ("parked_entitlements", "token"),
    ("token_fold_state", "token"),
    ("ledger_renet_tokens", "token"),
]


def _counts(cur) -> dict[str, int]:
    params = {"v2": SOURCE_V2, "w": WMON}
    cur.execute(
        """SELECT
            (SELECT COUNT(*) FROM launchpad_tokens WHERE source <> 0 AND quote_token = %(w)s),
            (SELECT COUNT(*) FROM launchpad_tokens WHERE source <> 0),
            (SELECT COUNT(*) FROM launchpad_tokens WHERE source = %(v2)s AND quote_token <> %(w)s)""",
        params,
    )
    mon, total, scoped = cur.fetchone()
    return {"mon_nadfun": int(mon), "all_nadfun": int(total), "scoped": int(scoped)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="drop nad.fun v2 tokens that launched on a curve quoted in anything but mon"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect", type=int, required=True, help="the scoped token count you checked beforehand")
    args = parser.parse_args()

    storage.init_pool()
    params = {"v2": SOURCE_V2, "w": WMON}
    with storage.db_cursor() as cur:
        before = _counts(cur)
        print(f"[NONMON] before: {before}")
        if before["scoped"] != args.expect:
            raise SystemExit(f"[NONMON] scope is {before['scoped']}, expected {args.expect}; refusing")

        removed = {}
        for table, col in TABLES:
            cur.execute(f"DELETE FROM {table} WHERE {col} IN {SCOPE}", params)
            removed[table] = cur.rowcount
        cur.execute(f"DELETE FROM launchpad_tokens WHERE token IN {SCOPE}", params)
        removed["launchpad_tokens"] = cur.rowcount

        after = _counts(cur)
        for name, count in removed.items():
            if count:
                print(f"  {name:<28} {count:>10,}")
        print(f"[NONMON] after:  {after}")

        problems = []
        if after["mon_nadfun"] != before["mon_nadfun"]:
            problems.append(f"mon-quoted nad.fun tokens changed {before['mon_nadfun']} -> {after['mon_nadfun']}")
        if after["scoped"] != 0:
            problems.append(f"{after['scoped']} scoped tokens survived")
        if before["all_nadfun"] - after["all_nadfun"] != before["scoped"]:
            problems.append("nad.fun shrank by more than the scoped tokens")
        if removed["launchpad_tokens"] != args.expect:
            problems.append(f"removed {removed['launchpad_tokens']} tokens, expected {args.expect}")
        if problems:
            cur.connection.rollback()
            raise SystemExit("[NONMON] rolled back: " + "; ".join(problems))

        total = sum(removed.values())
        if not args.apply:
            cur.connection.rollback()
            print(f"[NONMON] dry run, rolled back ({total:,} rows would go). pass --apply to commit")
            return
        cur.connection.commit()
        print(f"[NONMON] committed, {total:,} rows removed, mon-quoted nad.fun untouched")


if __name__ == "__main__":
    main()
