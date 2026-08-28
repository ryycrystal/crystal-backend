import argparse
from collections import Counter

import httpx

import core.storage as storage

GRAPH_URL = "https://api.goldsky.com/api/public/project_cmssodtytvdwv01onckcx2jvl/subgraphs/crystal/1.0.5/gn"

ACCOUNT_QUERY = """
query ($id: ID!) {
  account(id: $id) {
    id
    openOrderMap {
      shards(first: 1000) { batches(first: 1000) { orders(first: 1000) {
        id market { id } isBuy price originalSize remainingSize status placedAt updatedAt txHash
      }}}
    }
    orderMap {
      shards(first: 1000) { batches(first: 1000) { orders(first: 1000) {
        id market { id } isBuy price originalSize remainingSize status placedAt updatedAt txHash
      }}}
    }
    tradeMap {
      shards(first: 1000) { batches(first: 1000) { trades(first: 1000) {
        id market { id } amountIn amountOut startPrice endPrice isBuy timestamp tx
      }}}
    }
  }
}
"""


def _graphql(url: str, query: str, variables: dict) -> dict:
    resp = httpx.post(url, json={"query": query, "variables": variables}, timeout=30.0)
    resp.raise_for_status()
    out = resp.json()
    if out.get("errors"):
        raise RuntimeError(out["errors"][0].get("message", "graphql error"))
    return out.get("data") or {}


def _flatten(mp: dict | None, key: str) -> list[dict]:
    rows = []
    for shard in (mp or {}).get("shards") or []:
        for batch in shard.get("batches") or []:
            rows.extend(batch.get(key) or [])
    return rows


def fetch_goldsky_account(url: str, wallet: str) -> dict | None:
    for candidate in (wallet.lower(), wallet):
        data = _graphql(url, ACCOUNT_QUERY, {"id": candidate})
        if data.get("account"):
            return data["account"]
    return None


def _order_tail(oid: str) -> int:
    try:
        return int((oid or "").split(":")[-1])
    except ValueError:
        return 0


def _goldsky_orders(raw: list[dict]) -> dict[tuple[str, int, int], dict]:
    out = {}
    for o in raw:
        key = (
            ((o.get("market") or {}).get("id") or "").lower(),
            int(o.get("price") or 0),
            _order_tail(o.get("id") or ""),
        )
        out[key] = {
            "is_buy": bool(o.get("isBuy")),
            "price": int(o.get("price") or 0),
            "remaining": int(o.get("remainingSize") or 0),
            "status": o.get("status"),
        }
    return out


def _is_cloid(order_id: int) -> bool:
    return order_id >> 41 > 0


def _goldsky_trades(raw: list[dict]) -> Counter:
    return Counter(
        (
            (t.get("tx") or "").lower(),
            ((t.get("market") or {}).get("id") or "").lower(),
            bool(t.get("isBuy")),
            int(t.get("amountIn") or 0),
            int(t.get("amountOut") or 0),
            int(t.get("startPrice") or 0),
            int(t.get("endPrice") or 0),
            int(t.get("timestamp") or 0),
        )
        for t in raw
    )


def _backend_orders(wallet: str) -> dict[tuple[str, int, int], dict]:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT market, price, order_id, is_buy, size, status
            FROM crystal_orderbook_orders WHERE user_address = %s
            """,
            (wallet.lower(),),
        )
        rows = cur.fetchall()
    return {
        (m, int(p), int(oid)): {"is_buy": bool(b), "price": int(p), "remaining": int(s), "status": st}
        for m, p, oid, b, s, st in rows
    }


def _backend_trades(wallet: str) -> Counter:
    with storage.db_cursor() as cur:
        cur.execute(
            """
            SELECT txhash, market, is_buy, amount_in, amount_out, start_price, end_price, timestamp
            FROM crystal_market_trades WHERE user_address = %s
            UNION ALL
            SELECT txhash, market, maker_is_buy, amount_out, amount_high, price, price, timestamp
            FROM crystal_orderbook_fills WHERE maker = %s AND order_id < 2199023255552
            """,
            (wallet.lower(), wallet.lower()),
        )
        rows = cur.fetchall()
    return Counter((tx, m, bool(b), int(ai), int(ao), int(sp), int(ep), int(ts)) for tx, m, b, ai, ao, sp, ep, ts in rows)


def _rest(base: str, path: str) -> dict:
    resp = httpx.get(base.rstrip("/") + path, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _backend_orders_rest(base: str, wallet: str) -> dict[tuple[str, int, int], dict]:
    body = _rest(base, f"/orderbook/open/{wallet.lower()}")
    return {
        (o["market"], int(o["price"]), int(o["order_id"])): {
            "is_buy": bool(o["is_buy"]),
            "price": int(o["price"]),
            "remaining": int(o["size"]),
            "status": "open",
        }
        for o in body.get("orders") or []
    }


def _backend_trades_rest(base: str, wallet: str) -> Counter:
    rows: list[dict] = []
    before = None
    while True:
        path = f"/orderbook/trades/{wallet.lower()}?limit=500"
        if before is not None:
            path += f"&before_ts={before}"
        body = _rest(base, path)
        rows.extend(t for t in body.get("trades") or [] if t.get("kind") == "taker")
        before = body.get("next_before_ts")
        if before is None:
            break
    return Counter(
        (
            t["txhash"],
            t["market"],
            bool(t["is_buy"]),
            int(t["amount_in"]),
            int(t["amount_out"]),
            int(t["start_price"]),
            int(t["end_price"]),
            int(t["timestamp"]),
        )
        for t in rows
    )


def compare_wallet(wallet: str, graph_url: str, backend: str | None) -> int:
    print(f"\n=== {wallet} ===")
    acct = fetch_goldsky_account(graph_url, wallet)
    if acct is None:
        print("  goldsky: no account entity")
        gk_open: dict = {}
        gk_all: dict = {}
        gk_trades: Counter = Counter()
    else:
        gk_open = _goldsky_orders(_flatten(acct.get("openOrderMap"), "orders"))
        gk_all = _goldsky_orders(_flatten(acct.get("orderMap"), "orders"))
        gk_trades = _goldsky_trades(_flatten(acct.get("tradeMap"), "trades"))

    if backend:
        be_orders = _backend_orders_rest(backend, wallet)
        be_trades = _backend_trades_rest(backend, wallet)
    else:
        be_orders = _backend_orders(wallet)
        be_trades = _backend_trades(wallet)

    mismatches = 0

    gk_open_live = {k: v for k, v in gk_open.items() if v["remaining"] > 0}
    be_open = {k: v for k, v in be_orders.items() if v["status"] == "open" and v["remaining"] > 0}
    goldsky_gaps = 0
    for k in sorted(set(gk_open_live) | set(be_open)):
        g, b = gk_open_live.get(k), be_open.get(k)
        if g is None:
            if _is_cloid(k[2]):
                goldsky_gaps += 1
                continue
            print(f"  OPEN only in backend: {k} {b}")
            mismatches += 1
        elif b is None:
            print(f"  OPEN only in goldsky: {k} {g}")
            mismatches += 1
        elif (g["is_buy"], g["remaining"]) != (b["is_buy"], b["remaining"]):
            print(f"  OPEN field diff {k}: goldsky={g} backend={b}")
            mismatches += 1
    print(f"  open orders: goldsky={len(gk_open_live)} backend={len(be_open)} (cloid, goldsky-invisible: {goldsky_gaps})")

    missing = sorted(set(gk_all) - set(be_orders))
    for k in missing:
        print(f"  ORDER missing in backend: {k} {gk_all[k]}")
        mismatches += 1
    agree = 0
    for k in set(gk_all) & set(be_orders):
        g, b = gk_all[k], be_orders[k]
        g_live = k in gk_open_live
        b_live = b["status"] == "open" and b["remaining"] > 0
        if (g["is_buy"], g_live) != (b["is_buy"], b_live):
            print(f"  ORDER state diff {k}: goldsky={g} live={g_live} backend={b}")
            mismatches += 1
        else:
            agree += 1
    print(f"  order history: goldsky={len(gk_all)} backend={len(be_orders)} agreeing={agree}")

    for row, n in (gk_trades - be_trades).items():
        print(f"  TRADE only in goldsky x{n}: {row}")
        mismatches += 1
    for row, n in (be_trades - gk_trades).items():
        print(f"  TRADE only in backend x{n}: {row}")
        mismatches += 1
    print(f"  taker trades: goldsky={sum(gk_trades.values())} backend={sum(be_trades.values())}")

    print(f"  {'PARITY' if mismatches == 0 else f'{mismatches} MISMATCHES'}")
    return mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description="diff goldsky ordercenter data against the backend plane")
    parser.add_argument("wallets", nargs="+", help="wallet addresses to compare")
    parser.add_argument("--graph-url", default=GRAPH_URL)
    parser.add_argument("--backend", default=None, help="rest base url, omit to read the database directly")
    args = parser.parse_args()

    if not args.backend:
        storage.init_pool()

    total = 0
    for w in args.wallets:
        total += compare_wallet(w, args.graph_url, args.backend)
    print(f"\n{'ALL WALLETS AT PARITY' if total == 0 else f'{total} total mismatches'}")
    if total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
