import argparse
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.storage as storage
from core import chain as h
from core.sequencer import Sequencer
from core.storage import db_cursor
from state import RPC_HTTP

_code_cache: dict[str, bool] = {}
_receipt_cache: dict[str, list] = {}


def _rpc(method: str, params: list):
    resp = httpx.post(
        RPC_HTTP,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise ValueError(str(body["error"]))
    return body.get("result")


def is_contract(addr: str) -> bool:
    """True only for real contracts.

    An eip 7702 delegated wallet carries code too: the 23 byte designator
    0xef0100 followed by the implementation address. Those are still ordinary
    user wallets, and treating them as contracts would both flag real traders as
    suspects and block writing them back as the resolved trader.
    """
    a = (addr or "").lower()
    if a in _code_cache:
        return _code_cache[a]
    try:
        code = (_rpc("eth_getCode", [a, "latest"]) or "0x").lower()
        delegated = code.startswith("0xef0100") and len(code) == 48
        out = bool(code) and code != "0x" and not delegated
    except Exception:
        out = False
    _code_cache[a] = out
    return out


def receipt_logs(txh: str) -> list:
    t = (txh or "").lower()
    if t in _receipt_cache:
        return _receipt_cache[t]
    try:
        r = _rpc("eth_getTransactionReceipt", [t]) or {}
        logs = r.get("logs") or []
    except Exception:
        logs = []
    _receipt_cache[t] = logs
    return logs


def resolve_real_trader(seq, txh: str, token: str, current_user: str, is_buy: bool) -> str:
    logs = receipt_logs(txh)
    if not logs:
        return ""
    maps = seq._build_transfer_maps(logs)
    parsed = {"token": (token or "").lower(), "user": (current_user or "").lower(), "is_buy": bool(is_buy)}
    try:
        return (seq._resolve_trade_user(txh, parsed, h.CONTRACTS["ROUTER"].lower(), maps) or "").lower()
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="rewrite trades whose stored trader is a router or settler, using the same transfer graph walk the indexer does"
    )
    parser.add_argument("--apply", action="store_true", help="write the rows, default is a dry run")
    parser.add_argument("--table", default="launchpad_trades", choices=["launchpad_trades", "crystal_market_trades"])
    parser.add_argument("--token", default="", help="limit to one token")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    storage.init_pool()
    seq = object.__new__(Sequencer)

    where = "TRUE"
    params: list = []
    if args.token:
        where += " AND token = %s"
        params.append(args.token.lower())
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit > 0 else ""

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT user_address FROM {args.table}
            WHERE {where}
            """,
            params,
        )
        addrs = [r[0] for r in cur.fetchall() if r[0]]

    suspects = [a for a in addrs if is_contract(a)]
    print(f"[TRADERS] {len(addrs)} distinct traders, {len(suspects)} are contracts", flush=True)
    if not suspects:
        print("[TRADERS] nothing to repair", flush=True)
        return

    for a in suspects:
        print(f"[TRADERS]   contract trader: {a}", flush=True)

    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT txhash, log_index, token, user_address, is_buy
            FROM {args.table}
            WHERE user_address = ANY(%s) AND {where}
            ORDER BY block_number DESC
            {limit_sql}
            """,
            [suspects] + params,
        )
        rows = cur.fetchall()

    print(f"[TRADERS] {len(rows)} rows credited to a contract", flush=True)

    fixed = unresolved = unchanged = 0
    for txh, log_index, token, user_address, is_buy in rows:
        real = resolve_real_trader(seq, txh, token, user_address, is_buy)
        if not real:
            unresolved += 1
            continue
        if real == (user_address or "").lower():
            unchanged += 1
            continue
        if is_contract(real):
            # a smart account can legitimately be the trader, but a router that
            # only forwards should never survive the graph walk
            print(f"[TRADERS] {txh[:12]} resolved to another contract {real[:12]}, leaving it", flush=True)
            unchanged += 1
            continue

        print(f"[TRADERS] {txh[:12]}-{log_index} {user_address[:12]} -> {real[:12]}", flush=True)
        if args.apply:
            with db_cursor() as cur:
                cur.execute(
                    f"UPDATE {args.table} SET user_address = %s WHERE txhash = %s AND log_index = %s",
                    (real, txh, log_index),
                )
        fixed += 1

    verb = "repaired" if args.apply else "would repair"
    print(f"[TRADERS] {verb} {fixed}, unresolved {unresolved}, left alone {unchanged}", flush=True)


if __name__ == "__main__":
    main()
