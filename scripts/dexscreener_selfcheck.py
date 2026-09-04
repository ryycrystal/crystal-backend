import argparse
import json
import sys
import urllib.error
import urllib.request
from decimal import Decimal

FAILURES = []


def fail(msg):
    FAILURES.append(msg)
    print(f"FAIL {msg}")


def fetch(base, path, expect_status=200):
    url = base + path
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        fail(f"{url} unreachable: {e!r}")
        return 0, None


def check_dec(value, field, allow_zero=True):
    if not isinstance(value, str) or not value or "e" in value.lower():
        fail(f"{field} is not a plain decimal string: {value!r}")
        return
    try:
        d = Decimal(value)
    except Exception:
        fail(f"{field} does not parse as decimal: {value!r}")
        return
    if not allow_zero and d <= 0:
        fail(f"{field} must be > 0: {value!r}")
    frac = value.split(".")[1] if "." in value else ""
    if len(frac) > 50:
        fail(f"{field} exceeds 50 decimal places: {value!r}")


def check_block(obj, field):
    if not isinstance(obj, dict):
        fail(f"{field} missing block object: {obj!r}")
        return
    for key in ("blockNumber", "blockTimestamp"):
        if not isinstance(obj.get(key), int) or obj[key] <= 0:
            fail(f"{field}.{key} invalid: {obj.get(key)!r}")


def check_event(ev):
    tag = f"event {ev.get('txnId')}#{ev.get('eventIndex')}"
    check_block(ev.get("block"), tag)
    if ev.get("eventType") != "swap":
        fail(f"{tag} eventType != swap")
    if not isinstance(ev.get("txnId"), str) or not ev["txnId"]:
        fail(f"{tag} txnId invalid")
    for key in ("txnIndex", "eventIndex"):
        if not isinstance(ev.get(key), int) or ev[key] < 0:
            fail(f"{tag} {key} invalid: {ev.get(key)!r}")
    for key in ("maker", "pairId"):
        if not isinstance(ev.get(key), str) or not ev[key].startswith("0x"):
            fail(f"{tag} {key} invalid: {ev.get(key)!r}")
    check_dec(ev.get("priceNative"), f"{tag} priceNative", allow_zero=False)
    reserves = ev.get("reserves")
    if not isinstance(reserves, dict):
        fail(f"{tag} reserves missing")
    else:
        check_dec(reserves.get("asset0"), f"{tag} reserves.asset0")
        check_dec(reserves.get("asset1"), f"{tag} reserves.asset1")
    ins = [k for k in ("asset0In", "asset1In") if k in ev]
    outs = [k for k in ("asset0Out", "asset1Out") if k in ev]
    if ins not in (["asset0In"], ["asset1In"]) or outs not in (["asset0Out"], ["asset1Out"]):
        fail(f"{tag} must have exactly one In and one Out: {ins} {outs}")
    elif (ins, outs) not in ((["asset1In"], ["asset0Out"]), (["asset0In"], ["asset1Out"])):
        fail(f"{tag} inconsistent swap direction: {ins} {outs}")
    else:
        check_dec(ev[ins[0]], f"{tag} {ins[0]}", allow_zero=False)
        check_dec(ev[outs[0]], f"{tag} {outs[0]}", allow_zero=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--token", default=None)
    ap.add_argument("--scan-windows", type=int, default=50)
    ap.add_argument("--window", type=int, default=2000)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    status, body = fetch(base, "/dexscreener/latest-block")
    if status != 200 or not body:
        fail(f"latest-block returned {status}")
        return finish()
    check_block((body or {}).get("block"), "latest-block")
    tip = body["block"]["blockNumber"]
    print(f"latest-block ok: {body['block']}")

    status, body = fetch(base, f"/dexscreener/events?fromBlock={tip + 500000}&toBlock={tip + 500000}")
    if status != 200 or body != {"events": []}:
        fail(f"empty range should return {{'events': []}}, got {status} {body!r}")

    status, _ = fetch(base, "/dexscreener/asset?id=0x0000000000000000000000000000000000000001")
    if status != 404:
        fail(f"unknown asset should 404, got {status}")
    status, _ = fetch(base, "/dexscreener/pair?id=0x0000000000000000000000000000000000000001")
    if status != 404:
        fail(f"unknown pair should 404, got {status}")

    pair_id = args.token
    events_checked = 0
    for i in range(args.scan_windows):
        to_block = tip - i * args.window
        from_block = max(to_block - args.window + 1, 0)
        status, body = fetch(base, f"/dexscreener/events?fromBlock={from_block}&toBlock={to_block}")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("events"), list):
            fail(f"events {from_block}-{to_block} returned {status} {type(body)}")
            break
        events = body["events"]
        keys = [(e.get("block", {}).get("blockNumber"), e.get("txnIndex"), e.get("eventIndex")) for e in events]
        if keys != sorted(keys):
            fail(f"events {from_block}-{to_block} not ordered by (blockNumber, txnIndex, eventIndex)")
        if len(set(keys)) != len(keys):
            fail(f"events {from_block}-{to_block} contain duplicate (blockNumber, txnIndex, eventIndex)")
        for ev in events:
            check_event(ev)
            events_checked += 1
        if events and not pair_id:
            pair_id = events[-1]["pairId"]
        if events_checked and (pair_id or args.token):
            break
    print(f"events ok: {events_checked} events validated")
    if not events_checked:
        print("warn: no events found in scanned range, pass --token to check asset/pair")

    if pair_id:
        status, body = fetch(base, f"/dexscreener/asset?id={pair_id}")
        if status != 200 or "asset" not in (body or {}):
            fail(f"asset {pair_id} returned {status}")
        else:
            asset = body["asset"]
            for key in ("id", "name", "symbol"):
                if not isinstance(asset.get(key), str) or not asset[key]:
                    fail(f"asset.{key} empty: {asset.get(key)!r}")
            if "totalSupply" in asset:
                check_dec(asset["totalSupply"], "asset.totalSupply", allow_zero=False)
            if "circulatingSupply" in asset:
                check_dec(asset["circulatingSupply"], "asset.circulatingSupply")
            print(f"asset ok: {asset['symbol']} ({asset['id']})")

        status, body = fetch(base, f"/dexscreener/pair?id={pair_id}")
        if status != 200 or "pair" not in (body or {}):
            fail(f"pair {pair_id} returned {status}")
        else:
            pair = body["pair"]
            for key in ("id", "dexKey", "asset0Id", "asset1Id"):
                if not isinstance(pair.get(key), str) or not pair[key]:
                    fail(f"pair.{key} empty: {pair.get(key)!r}")
            for key in ("createdAtBlockNumber", "createdAtBlockTimestamp"):
                if not isinstance(pair.get(key), int) or pair[key] <= 0:
                    fail(f"pair.{key} invalid: {pair.get(key)!r}")
            print(f"pair ok: {pair['id']} quote {pair['asset1Id']}")

            status, body = fetch(base, f"/dexscreener/asset?id={pair['asset1Id']}")
            if status != 200 or "asset" not in (body or {}):
                fail(f"quote asset {pair['asset1Id']} returned {status}")
            else:
                print(f"quote asset ok: {body['asset']['symbol']}")

    return finish()


def finish():
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        sys.exit(1)
    print("\nall checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
