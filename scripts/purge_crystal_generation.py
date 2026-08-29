import argparse

import core.storage as storage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="drop retired crystal generation rows without touching nad.fun history",
    )
    parser.add_argument("--before-block", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    storage.init_pool()
    with storage.db_cursor() as cur:
        scope = storage.crystal_generation_counts(args.before_block, cur)
        before = storage.nadfun_row_counts(cur)
        print(f"[PURGE] crystal rows created before {args.before_block:,}: {scope}")
        print(f"[PURGE] nadfun before: {before}")

        removed = storage.delete_crystal_generation_before(args.before_block, cur)
        total = sum(removed.values())
        for name, count in removed.items():
            if count:
                print(f"  {name:<32} {count:>10,}")

        after = storage.nadfun_row_counts(cur)
        shrunk = {k: (before[k], after[k]) for k in before if after[k] < before[k]}
        print(f"[PURGE] nadfun after: {after}")

        if shrunk:
            cur.connection.rollback()
            raise SystemExit(f"[PURGE] rolled back, nadfun rows disappeared: {shrunk}")

        if not args.apply:
            cur.connection.rollback()
            print(f"[PURGE] dry run, rolled back ({total:,} rows would go). pass --apply to commit")
            return

        cur.connection.commit()
        print(f"[PURGE] committed, {total:,} crystal rows removed, nadfun untouched")


if __name__ == "__main__":
    main()
