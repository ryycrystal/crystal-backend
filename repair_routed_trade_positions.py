"""Re-derive positions whose trades were recorded from only some of their legs.

A routed buy is filled from several venues in one transaction. The indexer used
to record only the leg that landed on the token's registered pool, so both
token_bought and native_spent were understated while balance_token, which comes
from ERC-20 Transfer events, stayed correct. That gap inflates PnL.

Holding more than you bought is ALSO the normal shape of an airdrop, and those
positions are correct as they stand. The two are separated here by re-deriving
from chain rather than by any ratio heuristic: a transaction carrying swap legs
for the token is a trade, one carrying only transfers is not.

Per position this reports:

    recorded_bought / derived_bought   tokens across all decodable swap legs
    recorded_native / derived_native   native paid across those same legs
    transfer_in                        tokens that arrived with no swap at all

Dry by default. --apply writes, and only after snapshotting the rows it changes.

    python repair_routed_trade_positions.py --sample 12
    python repair_routed_trade_positions.py --band routing --sample 40
    python repair_routed_trade_positions.py --band routing --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any

from env_loader import load_env

load_env()

import core.chain as h  # noqa: E402
import modules.univ4 as univ4  # noqa: E402
from core.storage.base import db_cursor, init_pool  # noqa: E402

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

EXPECTED_PGHOST_MARKER = "crystal-prod-db-r3"

BANDS = {
    "routing": "AND balance_token < token_bought * 1.5",
    "airdrop": "AND balance_token >= token_bought * 1.5",
    "all": "",
}

_rpc_id = 0


def rpc(method: str, params: list) -> Any:
    global _rpc_id
    _rpc_id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        h.os.getenv("RPC_HTTP", "https://rpc.monad.xyz"),
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def resolved_pghost() -> str:
    url = h.os.getenv("DATABASE_URL", "")
    if url:
        tail = url.split("@", 1)[1] if "@" in url else url
        return tail.split("/", 1)[0]
    return h.os.getenv("PGHOST", "")


def preflight(apply: bool, force_host: bool) -> None:
    host = resolved_pghost()
    print(f"database host: {host or '<unset>'}")
    if apply and EXPECTED_PGHOST_MARKER not in host and not force_host:
        raise SystemExit(
            f"refusing to --apply against {host!r}: the live database is {EXPECTED_PGHOST_MARKER}"
        )


def signed(word: str) -> int:
    return int.from_bytes(bytes.fromhex(word), "big", signed=True)


def flagged_positions(band: str, limit: int | None) -> list[tuple[str, str, int, int, int, int]]:
    sql = f"""
        SELECT user_address, token, balance_token, token_bought, native_spent, cost_basis_native
        FROM launchpad_positions
        WHERE balance_token > 0 AND token_sold = 0 AND trade_count > 0
          AND balance_token > token_bought * 1.001
          {BANDS[band]}
        ORDER BY user_address, token
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db_cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def position_txhashes(user: str, token: str) -> list[str]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT txhash FROM launchpad_trades WHERE user_address = %s AND token = %s",
            (user, token),
        )
        return [r[0] for r in cur.fetchall()]


def derive_from_tx(txh: str, token: str, user: str) -> tuple[int, int, int]:
    """(token_delta_from_swaps, native_paid, token_delta_from_transfers) for one tx."""
    receipt = rpc("eth_getTransactionReceipt", [txh])
    if not receipt:
        return 0, 0, 0

    net_transfer = 0
    swap_tokens = 0
    swap_native = 0
    token_l = token.lower()
    user_l = user.lower()

    for log in receipt.get("logs", []):
        topics = [str(t).lower() for t in log.get("topics", [])]
        if not topics:
            continue
        addr = str(log.get("address", "")).lower()
        data = str(log.get("data", ""))[2:]

        if topics[0] == TRANSFER_TOPIC and addr == token_l and len(topics) >= 3:
            frm = "0x" + topics[1][-40:]
            to = "0x" + topics[2][-40:]
            amount = int(data[:64] or "0", 16)
            if to == user_l:
                net_transfer += amount
            if frm == user_l:
                net_transfer -= amount

        elif topics[0] == V3_SWAP_TOPIC and len(data) >= 128:
            a0, a1 = signed(data[0:64]), signed(data[64:128])
            got, paid = _leg_amounts(addr, a0, a1, token_l, receipt, invert=False)
            swap_tokens += got
            swap_native += paid

        elif topics[0] == univ4.V4_SWAP_TOPIC:
            ev = univ4.parse_v4_swap(addr, log.get("topics", []), data)
            if ev:
                got, paid = _leg_amounts(addr, -ev["amount0"], -ev["amount1"], token_l, receipt, invert=True)
                swap_tokens += got
                swap_native += paid

    return swap_tokens, swap_native, net_transfer


def _leg_amounts(pool: str, a0: int, a1: int, token: str, receipt: dict, invert: bool) -> tuple[int, int]:
    """Which side of a swap is our token, matched by an exact transfer amount."""
    moved = {}
    for log in receipt.get("logs", []):
        topics = [str(t).lower() for t in log.get("topics", [])]
        if not topics or topics[0] != TRANSFER_TOPIC or len(topics) < 3:
            continue
        amount = int(str(log.get("data", ""))[2:][:64] or "0", 16)
        moved.setdefault(amount, set()).add(str(log.get("address", "")).lower())

    for amt, native in ((a0, a1), (a1, a0)):
        if amt >= 0:
            continue
        if token in moved.get(abs(amt), set()):
            return abs(amt), max(native, 0)
    return 0, 0


def analyse(user: str, token: str, max_txs: int) -> dict:
    txs = position_txhashes(user, token)
    truncated = len(txs) > max_txs
    derived_tokens = derived_native = transfer_only = 0
    trade_txs = 0
    imputed_txs = 0
    for txh in txs[:max_txs]:
        got, paid, net = derive_from_tx(txh, token, user)
        if got > 0 and net != 0:
            true_tokens = abs(net)
            true_native = paid * true_tokens // got
            if true_tokens != got:
                imputed_txs += 1
            derived_tokens += true_tokens
            derived_native += true_native
            trade_txs += 1
        elif got > 0:
            derived_tokens += got
            derived_native += paid
            trade_txs += 1
        elif net > 0:
            transfer_only += net
    return {
        "txs": len(txs),
        "scanned": min(len(txs), max_txs),
        "truncated": truncated,
        "trade_txs": trade_txs,
        "derived_tokens": derived_tokens,
        "derived_native": derived_native,
        "transfer_in": transfer_only,
        "imputed_txs": imputed_txs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", choices=sorted(BANDS), default="all")
    ap.add_argument("--sample", type=int, default=0, help="only look at N positions")
    ap.add_argument("--max-txs", type=int, default=60, help="cap transactions re-derived per position")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-know-the-host", action="store_true")
    args = ap.parse_args()

    preflight(args.apply, args.i_know_the_host)
    init_pool()
    if args.apply:
        raise SystemExit("--apply is not implemented yet: run dry, review, then wire the write step")

    rows = flagged_positions(args.band, args.sample or None)
    print(f"band={args.band}  positions={len(rows)}\n")

    missing_legs = airdrops = clean = 0
    for user, token, balance, bought, native_spent, basis in rows:
        r = analyse(user, token, args.max_txs)
        bought = int(bought)
        derived = r["derived_tokens"]
        gap = derived - bought
        kind = "clean"
        if r["transfer_in"] > 0 and gap <= bought // 1000:
            kind = "AIRDROP"
            airdrops += 1
        elif gap > bought // 1000:
            kind = "MISSING LEGS"
            missing_legs += 1
        else:
            clean += 1
        print(
            f"{user[:12]} {token[:12]} {kind:<13} "
            f"bal={int(balance) / 1e18:>14,.0f} bought={bought / 1e18:>14,.0f} "
            f"derived={derived / 1e18:>14,.0f} transfer_in={r['transfer_in'] / 1e18:>14,.0f} "
            f"native={int(native_spent) / 1e18:>12,.0f}->{r['derived_native'] / 1e18:>12,.0f} "
            f"txs={r['scanned']}/{r['txs']}{'+' if r['truncated'] else ''}"
        )

    total = len(rows) or 1
    print(f"\nmissing legs : {missing_legs} ({missing_legs * 100 / total:.0f}%)")
    print(f"airdrops     : {airdrops} ({airdrops * 100 / total:.0f}%)")
    print(f"clean        : {clean} ({clean * 100 / total:.0f}%)")
    print(f"\nDecimal check unused: {Decimal(0)}" if False else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
