import argparse
from decimal import Decimal

import core.storage as storage

PRICE_MARKER = "sweep_graduated_price_at"
FEE_MARKER = "sweep_graduated_fees_at"


def stale_priced_tokens():
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT t.token, t.symbol, t.last_price_native,
                   lp.reserve_native / lp.reserve_token AS pool_price,
                   lp.last_sync_at,
                   (SELECT MAX(timestamp) FROM launchpad_trades tr WHERE tr.token = t.token) AS last_trade
            FROM launchpad_tokens t
            JOIN launchpad_pools lp ON lp.token_addr = t.token
            WHERE t.migrated AND lp.reserve_token > 0 AND lp.reserve_native > 0
              AND lp.last_sync_at > COALESCE(
                    (SELECT MAX(timestamp) FROM launchpad_trades tr WHERE tr.token = t.token), 0)
            ORDER BY t.volume_usd DESC
            """
        )
        return cur.fetchall()


def missing_fee_rows():
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT t.token, t.symbol, t.source, t.fees_usd,
                   COALESCE(SUM(tr.usd_amount) FILTER (WHERE tr.timestamp > t.migrated_at), 0) AS post_vol,
                   COALESCE(pf.creator_fee_rate, 0) + COALESCE(pf.dex_protocol_fee_rate, 0) AS pair_rate,
                   cm.taker_fee
            FROM launchpad_tokens t
            LEFT JOIN launchpad_trades tr ON tr.token = t.token
            LEFT JOIN launchpad_pair_fees pf ON LOWER(pf.pair) = LOWER(t.market) AND pf.ok
            LEFT JOIN crystal_markets cm ON LOWER(cm.market) = LOWER(t.market)
            WHERE t.migrated AND t.migrated_at > 0
            GROUP BY t.token, t.symbol, t.source, t.fees_usd, pf.creator_fee_rate, pf.dex_protocol_fee_rate, cm.taker_fee
            HAVING COALESCE(SUM(tr.usd_amount) FILTER (WHERE tr.timestamp > t.migrated_at), 0) > 0
            ORDER BY 5 DESC
            """
        )
        return cur.fetchall()


def fee_rate_for(source, pair_rate, taker_fee):
    if int(source or 0) == 0:
        if taker_fee is None:
            return None
        rate = (Decimal(100000) - Decimal(taker_fee)) / Decimal(100000)
        return rate if rate > 0 else None
    rate = Decimal(pair_rate or 0) / Decimal(10000)
    return rate if Decimal(0) < rate < Decimal("0.05") else None


def run_price(apply_changes, limit):
    rows = stale_priced_tokens()
    print(f"[SWEEP] {len(rows)} migrated tokens have reserves newer than their last indexed trade")
    shown = 0
    updates = []
    for token, symbol, old_px, pool_px, sync_at, last_trade in rows:
        old_px = Decimal(old_px or 0)
        pool_px = Decimal(pool_px or 0)
        if pool_px <= 0:
            continue
        updates.append((token, pool_px))
        if shown < limit:
            ratio = float(old_px / pool_px) if pool_px > 0 and old_px > 0 else 0
            print(f"  {symbol[:12]:<12} {token[:12]}.. stored={float(old_px):.12f} pool={float(pool_px):.12f} was {ratio:,.1f}x high")
            shown += 1
    if not apply_changes:
        print(f"[SWEEP] dry run, {len(updates)} rows would change")
        return
    with storage.db_cursor() as cur:
        for token, px in updates:
            cur.execute("UPDATE launchpad_tokens SET last_price_native = %s WHERE token = %s", (px, token))
        storage.set_meta(PRICE_MARKER, str(len(updates)), cur=cur)
    print(f"[SWEEP] updated {len(updates)} prices")


def run_fees(apply_changes, limit, force):
    if storage.get_meta(FEE_MARKER) and not force:
        raise RuntimeError(f"[SWEEP] fee backfill already ran ({FEE_MARKER} set); pass --force to run it again")
    rows = missing_fee_rows()
    updates = []
    skipped = 0
    total_added = Decimal(0)
    shown = 0
    for token, symbol, source, fees, post_vol, pair_rate, taker_fee in rows:
        rate = fee_rate_for(source, pair_rate, taker_fee)
        if rate is None:
            skipped += 1
            continue
        added = Decimal(post_vol or 0) * rate
        if added <= 0:
            continue
        updates.append((token, added))
        total_added += added
        if shown < limit:
            print(f"  {str(symbol)[:12]:<12} src={source} post_vol=${float(post_vol):>14,.2f} rate={float(rate):.5f} +${float(added):>12,.2f}")
            shown += 1
    print(f"[SWEEP] {len(updates)} tokens gain fees, {skipped} skipped for no known rate, total +${float(total_added):,.2f}")
    if not apply_changes:
        print("[SWEEP] dry run, nothing written")
        return
    with storage.db_cursor() as cur:
        for token, added in updates:
            cur.execute("UPDATE launchpad_tokens SET fees_usd = fees_usd + %s WHERE token = %s", (added, token))
        storage.set_meta(FEE_MARKER, str(len(updates)), cur=cur)
    print(f"[SWEEP] credited {len(updates)} tokens")


def main():
    ap = argparse.ArgumentParser(description="repair graduated tokens' stale prices and missing post-migration fees")
    ap.add_argument("--price", action="store_true")
    ap.add_argument("--fees", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not args.price and not args.fees:
        ap.error("pick --price, --fees, or both")
    storage.init_pool()
    if args.price:
        run_price(args.apply, args.limit)
    if args.fees:
        run_fees(args.apply, args.limit, args.force)


if __name__ == "__main__":
    main()
