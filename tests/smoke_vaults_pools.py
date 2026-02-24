import argparse
import json
import math
import sys
import urllib.parse
import urllib.request


ZERO = "0x0000000000000000000000000000000000000000"


def _get_json(base_url: str, path: str, params: dict | None = None):
    q = ""
    if params:
        q = "?" + urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{path}{q}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def _post_json(base_url: str, path: str, params: dict | None = None):
    q = ""
    if params:
        q = "?" + urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{path}{q}"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def _assert(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(float(v))


def _assert_series_sorted(series, key="timestamp"):
    prev = None
    for row in series or []:
        _assert(isinstance(row, dict), f"series row must be object: {row!r}")
        _assert(key in row, f"series row missing {key}: {row!r}")
        cur = int(row[key] or 0)
        if prev is not None:
            _assert(cur >= prev, f"series not ascending by {key}: {prev} -> {cur}")
        prev = cur


def _check_sync(base_url: str):
    try:
        data = _get_json(base_url, "/sync")
        _assert(isinstance(data, dict), "/sync response must be object")
        print(f"[smoke] /sync ok keys={sorted(list(data.keys()))[:8]}")
    except Exception as e:
        print(f"[smoke] /sync skipped ({e})")


def _check_vaults(base_url: str, vault_addr: str | None):
    lst = _get_json(
        base_url,
        "/vaults/list",
        {
            "user": ZERO,
            "status": "all",
            "page": 1,
            "limit": 5,
            "include_snapshot": 1,
            "snapshot_timeframe": 1,
            "snapshot_points": 24,
        },
    )
    _assert(isinstance(lst, dict), "/vaults/list must return object")
    _assert(lst.get("ok") is True, "/vaults/list ok must be true")
    _assert(isinstance(lst.get("vaults"), list), "/vaults/list.vaults must be list")
    if "count" in lst:
        _assert(int(lst["count"]) == len(lst["vaults"]), "/vaults/list count mismatch")
    print(f"[smoke] /vaults/list ok count={len(lst['vaults'])}")

    chosen = vault_addr.lower() if vault_addr else None
    if not chosen and lst["vaults"]:
        chosen = str(lst["vaults"][0].get("address") or "").lower()
    if not chosen:
        print("[smoke] vault detail/history skipped (no vaults)")
        return

    summary = _get_json(
        base_url,
        f"/vaults/{chosen}/{ZERO}",
        {"snapshot_timeframe": 1, "history_limit": 10},
    )
    _assert(isinstance(summary, dict), "vault summary must return object")
    _assert("ok" in summary, "vault summary missing ok")
    _assert(summary.get("ok") is True, "vault summary not ok")
    _assert(isinstance(summary.get("vault"), dict), "vault summary missing vault object")
    _assert(isinstance(summary.get("status"), dict), "vault summary missing status")
    _assert(isinstance(summary.get("latestBalance"), dict), "vault summary missing latestBalance")
    _assert(isinstance(summary.get("userBalance"), dict), "vault summary missing userBalance")
    _assert(_is_num(summary.get("tvlUsd", 0.0)), "vault summary tvlUsd not numeric")
    _assert(_is_num(summary["latestBalance"].get("usdValue", 0.0)), "vault latestBalance.usdValue not numeric")
    _assert(abs(float(summary.get("tvlUsd", 0.0)) - float(summary["latestBalance"].get("usdValue", 0.0))) < 1e-9, "vault tvlUsd != latestBalance.usdValue")
    if summary.get("snapshot") is not None:
        snap = summary["snapshot"]
        _assert(isinstance(snap.get("tvl"), list), "vault snapshot.tvl must be list")
        for p in snap["tvl"]:
            _assert(isinstance(p, list) and len(p) == 2, "vault snapshot point must be [ts, value]")
        _assert_series_sorted([{"timestamp": p[0]} for p in snap["tvl"]])
    print(f"[smoke] /vaults/{{addr}}/{{user}} ok vault={chosen}")

    hist = _get_json(base_url, f"/vaults/{chosen}/history/1")
    _assert(hist.get("ok") is True, "vault history not ok")
    _assert(isinstance(hist.get("series"), dict), "vault history missing series")
    _assert_series_sorted(hist["series"].get("tvl", []))
    _assert_series_sorted(hist["series"].get("pnl", []))
    print(f"[smoke] /vaults/{{addr}}/history/1 ok points={len(hist['series'].get('tvl', []))}")

    try:
        refreshed = _post_json(base_url, f"/vaults/{chosen}/refresh-balance", {"user": ZERO})
        _assert(isinstance(refreshed, dict), "refresh-balance must return object")
        _assert("vaultBalance" in refreshed or "latestBalance" in refreshed or "ok" in refreshed, "refresh-balance unexpected shape")
        print(f"[smoke] /vaults/{{addr}}/refresh-balance ok vault={chosen}")
    except Exception as e:
        print(f"[smoke] refresh-balance skipped ({e})")


def _check_pools(base_url: str, pool_addr: str | None):
    lst = _get_json(
        base_url,
        "/pools/list",
        {"page": 1, "limit": 5, "sort": "volume", "order": "desc"},
    )
    _assert(isinstance(lst, dict), "/pools/list must return object")
    _assert(isinstance(lst.get("pools"), list), "/pools/list.pools must be list")
    if "count" in lst:
        _assert(int(lst["count"]) == len(lst["pools"]), "/pools/list count mismatch")
    print(f"[smoke] /pools/list ok count={len(lst['pools'])}")

    chosen = pool_addr.lower() if pool_addr else None
    if not chosen and lst["pools"]:
        chosen = str((lst["pools"][0].get("address") or lst["pools"][0].get("market") or "")).lower()
    if not chosen:
        print("[smoke] pool detail skipped (no pools)")
        return

    pool = _get_json(base_url, f"/pools/{chosen}")
    _assert(isinstance(pool, dict), "pool detail must be object")
    for k in ("market", "reserveQuote", "reserveBase", "tvlUsd", "volume24hUsd", "fees24hUsd", "apy24hPercent", "dailyYieldPercent", "tvlHistory", "apyHistory"):
        _assert(k in pool, f"pool detail missing {k}")
    int(str(pool["reserveQuote"]))
    int(str(pool["reserveBase"]))
    _assert(_is_num(pool["tvlUsd"]), "pool tvlUsd not numeric")
    _assert(_is_num(pool["volume24hUsd"]), "pool volume24hUsd not numeric")
    _assert(_is_num(pool["fees24hUsd"]), "pool fees24hUsd not numeric")
    _assert(_is_num(pool["apy24hPercent"]), "pool apy24hPercent not numeric")
    _assert(_is_num(pool["dailyYieldPercent"]), "pool dailyYieldPercent not numeric")
    _assert_series_sorted(pool.get("tvlHistory", []))
    _assert_series_sorted(pool.get("apyHistory", []))
    if len(pool.get("tvlHistory", [])) == len(pool.get("apyHistory", [])):
        for a, b in zip(pool["tvlHistory"], pool["apyHistory"]):
            _assert(int(a["timestamp"]) == int(b["timestamp"]), "tvlHistory/apyHistory timestamps mismatch")

    vol = float(pool.get("volume24hUsd") or 0.0)
    fees = float(pool.get("fees24hUsd") or 0.0)
    if vol > 0:
        _assert(abs(fees - (vol * 0.0025)) < max(1e-9, vol * 1e-6), "fees24hUsd != volume24hUsd * 0.25%")
    apy_pct = float(pool.get("apy24hPercent") or 0.0)
    daily_pct = float(pool.get("dailyYieldPercent") or 0.0)
    _assert(abs(apy_pct - (daily_pct * 365.0)) < 1e-6, "apy24hPercent != dailyYieldPercent * 365")
    print(f"[smoke] /pools/{{addr}} ok pool={chosen}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--vault", default=None)
    ap.add_argument("--pool", default=None)
    args = ap.parse_args()

    _check_sync(args.base_url)
    _check_vaults(args.base_url, args.vault)
    _check_pools(args.base_url, args.pool)
    print("[smoke] vaults+pools smoke passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[smoke] failed: {e}", file=sys.stderr)
        sys.exit(1)
