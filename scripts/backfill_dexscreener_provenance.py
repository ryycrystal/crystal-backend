"""Stamp venue and tx_index on the crystal (source 0) trade rows, and repair
market-trade rows whose log_index was recorded as a loop position.

Rows written before venue existed carry NULL, so the dexscreener adapter falls
back to a reserve heuristic to tell a curve trade from a same-block market trade.
This reads each row's receipt and records what the transaction actually
contained: a LaunchpadTrade log makes the row a curve trade, a Crystal Trade log
makes it a market trade. Market rows written before fe8835f were keyed by their
position in the fetched log list rather than the on-chain logIndex; those are
re-keyed to the real index when exactly one matching log exists in the receipt.

Dry by default. --apply writes after snapshotting every row it changes.

    python scripts/backfill_dexscreener_provenance.py
    python scripts/backfill_dexscreener_provenance.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request

import psycopg2

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from env_loader import load_env  # noqa: E402

load_env()

import modules.launchpad as lp_mod  # noqa: E402
import modules.markets as mk_mod  # noqa: E402

LT_TOPIC = "0xc367a2f5396f96d105baaaa90fe29b1bb18ef54c712964410d02451e67c19d3e"
TR_TOPIC = "0x9adcf0ad0cda63c4d50f26a48925cf6405df27d422a39c456b5f03f661c82982"
EXPECTED_PGHOST_MARKER = "crystal-prod-db-r3"
SNAPSHOT = os.environ.get("BACKFILL_SNAPSHOT", "dexscreener_provenance_snapshot.jsonl")

_rpc_id = 0


def rpc(method, params):
    global _rpc_id
    last = None
    for attempt in range(6):
        _rpc_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}).encode()
        req = urllib.request.Request(
            os.getenv("RPC_HTTP", "https://rpc.monad.xyz"), data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read())
        except Exception as exc:
            last = exc
            time.sleep(min(2**attempt, 30))
            continue
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]
    raise RuntimeError(f"{method}: gave up: {last!r}")


def connect():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=int(os.getenv("PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"],
        sslmode=os.getenv("PGSSLMODE", "require"),
        connect_timeout=30,
    )


def hexint(v):
    return int(v, 16) if isinstance(v, str) else int(v)


def classify(receipt, token, market, native_amt, token_amt):
    curve = []
    market_logs = []
    for log in receipt.get("logs", []):
        topics = [str(t).lower() for t in log.get("topics", [])]
        if not topics:
            continue
        data = str(log.get("data", ""))[2:]
        if topics[0] == LT_TOPIC:
            ev = lp_mod.parse_launchpad_trade(log["address"].lower(), topics, data)
            if (ev.get("token") or "").lower() == token:
                curve.append((hexint(log["logIndex"]), ev))
        elif topics[0] == TR_TOPIC:
            ev = mk_mod.parse_trade(log["address"].lower(), topics, data)
            if market and (ev.get("market") or "").lower() == market:
                market_logs.append((hexint(log["logIndex"]), ev))
    return curve, market_logs


def match(cands, native_amt, token_amt):
    if len(cands) == 1:
        return cands[0][0]
    for idx, ev in cands:
        amounts = {int(ev.get("amount_in") or 0), int(ev.get("amount_out") or 0)}
        if native_amt in amounts and token_amt in amounts:
            return idx
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    host = os.getenv("PGHOST", "")
    print(f"database host: {host}")
    if args.apply and EXPECTED_PGHOST_MARKER not in host:
        raise SystemExit(f"refusing to --apply against {host!r}")

    conn = connect()
    conn.set_session(autocommit=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'launchpad_trades'"
    )
    have = {r[0] for r in cur.fetchall()}
    stamped = "venue" in have and "tx_index" in have
    extra = "t.venue, t.tx_index" if stamped else "NULL::text, NULL::integer"
    cur.execute(
        f"""
        SELECT t.id, t.txhash, t.log_index, t.token, k.market, t.native_amount, t.token_amount, {extra}
        FROM launchpad_trades t JOIN launchpad_tokens k ON k.token = t.token
        WHERE k.source = 0
        ORDER BY t.block_number, t.log_index
        """
    )
    rows = cur.fetchall()
    if not stamped:
        print("venue/tx_index columns not present yet (indexer migration pending); dry run only")
        if args.apply:
            raise SystemExit("cannot --apply before the indexer has created venue/tx_index")
    conn.close()
    print(f"{len(rows)} source-0 trade rows")

    plans = []
    receipts = {}
    for rid, txh, log_index, token, market, native_amt, token_amt, venue, tx_index in rows:
        if txh not in receipts:
            receipts[txh] = rpc("eth_getTransactionReceipt", [txh]) or {}
        receipt = receipts[txh]
        txi = hexint(receipt["transactionIndex"]) if receipt.get("transactionIndex") is not None else None
        curve, market_logs = classify(receipt, token.lower(), (market or "").lower(), int(native_amt), int(token_amt))
        curve_idx = {i for i, _ in curve}
        new_venue = None
        new_log_index = None
        if int(log_index) in curve_idx:
            new_venue = "curve"
        else:
            m = match(market_logs, int(native_amt), int(token_amt))
            if m is not None:
                new_venue = "market"
                new_log_index = m if m != int(log_index) else None
            elif match(curve, int(native_amt), int(token_amt)) is not None:
                new_venue = "curve"
                new_log_index = match(curve, int(native_amt), int(token_amt))
        if new_venue is None:
            print(f"  ? {txh[:14]}#{log_index} no matching curve/market log, left untouched")
            continue
        plans.append((rid, txh, int(log_index), new_venue, txi, new_log_index, venue, tx_index))
        flag = f" log_index {log_index}->{new_log_index}" if new_log_index is not None else ""
        print(f"  {txh[:14]}#{log_index:<4} venue={new_venue:<6} tx_index={txi}{flag}")

    fixes = sum(1 for p in plans if p[5] is not None)
    print(f"\n{len(plans)} rows would be stamped, {fixes} log_index repairs")
    if not args.apply:
        return

    conn = connect()
    with conn, conn.cursor() as cur:
        for rid, txh, old_idx, new_venue, txi, new_idx, old_venue, old_txi in plans:
            if new_idx is not None:
                cur.execute("SELECT 1 FROM launchpad_trades WHERE txhash = %s AND log_index = %s", (txh, new_idx))
                if cur.fetchone():
                    print(f"  ! {txh[:14]}#{old_idx}: {new_idx} already taken, skipping re-key")
                    new_idx = None
            with open(SNAPSHOT, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "id": rid,
                            "txhash": txh,
                            "log_index": old_idx,
                            "venue": old_venue,
                            "tx_index": old_txi,
                        }
                    )
                    + chr(10)
                )
            cur.execute(
                "UPDATE launchpad_trades SET venue = %s, tx_index = %s, log_index = COALESCE(%s, log_index) WHERE id = %s",
                (new_venue, txi, new_idx, rid),
            )
    conn.close()
    print(f"applied. snapshot: {SNAPSHOT}")


if __name__ == "__main__":
    main()
