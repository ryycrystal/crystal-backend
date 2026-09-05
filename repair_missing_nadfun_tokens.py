"""Re-insert nad.fun tokens whose TokenCreated log was never indexed.

Why these are missing
---------------------
A nad.fun generation that is added to the indexer's address filter after it has
already been live leaves a hole: the raw log cache is written address filtered,
so nothing was ever cached for that contract over the earlier range and a replay
cannot recover it either.

Tokens still trading on their curve heal themselves anyway, because a curve trade
for an unknown token goes through State._ensure_launchpad_token_locked. Tokens
that had already GRADUATED never get that chance: trading has moved to the pool,
the curve never emits for them again, and apply_migrated used to bail on an
unknown token so the pool was never registered either. They stay invisible
forever.

What this does
--------------
Seeds the identity that was lost, per token, straight from chain:

  * finds the creation block from the token's creation timestamp,
  * pulls the real TokenCreated log and decodes it with the indexer's own
    parser, so the row shape cannot drift from the live write path,
  * writes launchpad_tokens (with the correct nad.fun source), marks the v2
    generation, and stores token_uri so the indexer's metadata sweep fills in
    the image and socials on its own,
  * registers the pool and marks the token migrated when the graduation log is
    in the scanned window.

What this does NOT do
---------------------
It does not recover the trades those tokens made after graduating. Those were
pool swaps against a pool the indexer had not registered, so they were dropped
at the time and only a re-fetch and reindex of that block range brings them
back. After this runs the token exists, is searchable, and indexes correctly
from here on, but its history starts now.

Idempotent: every write is an upsert or a fill-if-empty, so re-running is safe.

    python repair_missing_nadfun_tokens.py --discover
    python repair_missing_nadfun_tokens.py --tokens 0x43cf...,0xeabf...
    python repair_missing_nadfun_tokens.py --discover --apply
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
import core.storage.launchpad as storage  # noqa: E402
import core.adapters.nadfun as nadfun_geo  # noqa: E402
import modules.nadfun as nadfun  # noqa: E402
from core.storage.base import db_cursor, init_pool  # noqa: E402

# replay floor from STARTUP_MODES.md; no launchpad token predates it
REPLAY_START_BLOCK = 37709836

# nad.fun v2 graduates into ordinary uniswap-v2 style pairs from this factory, so
# a pool the graduation log did not hand us can be asked for directly instead of
# scanning millions of blocks for the event
NADFUN_V2_FACTORY = h._addr_from_env(
    "0xa25b13127e63ddae6d0b35570ff3d39dbd621001", "NADFUN_V2_FACTORY_ADDRESS"
)

NADFUN_API = "https://api.nad.fun"
CRYSTAL_API = "https://crystal-api.yellowfield-3f176fc9.japaneast.azurecontainerapps.io"

# nad.fun's edge rejects the default urllib agent
_HTTP_HEADERS = {"User-Agent": "crystal-backend-repair/1.0"}

CREATE_TOPICS = {
    "0xd37e3f4f651fe74251701614dbeac478f5a0d29068e87bbe44e5026d166abca9",
    nadfun.V2_CREATE_TOPIC,
}
GRADUATE_TOPICS = {
    "0xa1cae252e597e19f398a442722a17a17e62d17f9d4f3656786e18aabcd428908",
    "0xc0682ac2d5a530c92664a8717db0ab335b5c5cbdfdc740185679e170244633e0",
    "0x381d54fa425631e6266af114239150fae1d5db67bb65b4fa9ecc65013107e07e",
}

# this rpc caps eth_getLogs at ~100 blocks, and the timestamp search lands within
# a block or two of the creation, so one call either side of it is plenty
WINDOW_BEHIND = 20
WINDOW_AHEAD = 79

_rpc_id = 0


def _http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def rpc(method: str, params: list) -> Any:
    global _rpc_id
    _rpc_id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(h.os.getenv("RPC_HTTP", "https://rpc.monad.xyz"), data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def block_timestamp(block: int) -> int:
    return int(rpc("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)


def head_block() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def block_at_timestamp(target: int) -> int:
    """First block whose timestamp is >= target."""
    lo, hi = REPLAY_START_BLOCK, head_block()
    while lo < hi:
        mid = (lo + hi) // 2
        if block_timestamp(mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def topic_addr(topic: str) -> str:
    return ("0x" + topic[-40:]).lower()


def eth_call(to: str, data: str) -> str:
    try:
        return rpc("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception:  # noqa: BLE001
        return ""


def _addr_from_word(word: str) -> str:
    if not word or len(word) < 42:
        return ""
    addr = ("0x" + word[-40:]).lower()
    return "" if addr == "0x" + "0" * 40 else addr


def pair_for(token: str, quote: str) -> str:
    """getPair(token, quote) on the v2 factory, empty when there is none."""
    if not quote:
        return ""
    data = "0xe6a43905" + token[2:].rjust(64, "0") + quote[2:].rjust(64, "0")
    return _addr_from_word(eth_call(NADFUN_V2_FACTORY, data))


def pair_token0(pair: str) -> str:
    """token0() of a pair. Read rather than inferred from address sort order, so
    the reserve orientation written to crystal_pools cannot be guessed wrong."""
    return _addr_from_word(eth_call(pair, "0x0dfe1681"))


def nadfun_token_info(token: str) -> dict | None:
    try:
        return _http_json(f"{NADFUN_API}/token/{token}").get("token_info")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! nad.fun lookup failed for {token}: {exc}")
        return None


EXPECTED_PGHOST_MARKER = "crystal-prod-db-r3"


def resolved_pghost() -> str:
    url = h.os.getenv("DATABASE_URL", "")
    if url:
        tail = url.split("@", 1)[1] if "@" in url else url
        return tail.split("/", 1)[0]
    return h.os.getenv("PGHOST", "")


def preflight(apply: bool, force_host: bool) -> None:
    host = resolved_pghost()
    print(f"database host: {host or '<unset>'}")
    if not apply:
        return
    if EXPECTED_PGHOST_MARKER not in host:
        if not force_host:
            raise SystemExit(
                f"refusing to --apply against {host!r}: the live database is "
                f"{EXPECTED_PGHOST_MARKER}. an older crystal-prod-db host still "
                "accepts connections and is stale. fix PGHOST/DATABASE_URL, or pass "
                "--i-know-the-host if this really is the intended target."
            )
        print(f"WARNING: applying against {host!r}, which is not {EXPECTED_PGHOST_MARKER}")


def present_in_db(token: str) -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM launchpad_tokens WHERE token = %s", (token.lower(),))
        return cur.fetchone() is not None


def present_in_api(token: str) -> bool:
    try:
        return int(_http_json(f"{CRYSTAL_API}/search/query?query={token}").get("total", 0)) > 0
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"presence check failed for {token}: {exc}") from exc


def discover_candidates(pages: int, limit: int) -> list[str]:
    """nad.fun tokens, newest and largest first. Cheap, and the missing tokens
    are graduated so they surface on the market cap ranking."""
    seen: dict[str, None] = {}
    for feed in ("order/market_cap", "order/creation_time"):
        for page in range(1, pages + 1):
            try:
                body = _http_json(f"{NADFUN_API}/{feed}?page={page}&limit={limit}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {feed} page {page} failed: {exc}")
                break
            for entry in body.get("tokens", []):
                info = entry.get("token_info", entry)
                addr = str(info.get("token_id", "")).lower()
                if addr:
                    seen.setdefault(addr, None)
    return list(seen)


def find_creation(token: str, created_at: int) -> tuple[dict | None, dict | None, int]:
    """(create_log, graduate_log, block) for a token, searching around its
    creation timestamp. graduate_log is None unless it lands in the window."""
    approx = block_at_timestamp(created_at)
    lo, hi = max(REPLAY_START_BLOCK, approx - WINDOW_BEHIND), approx + WINDOW_AHEAD
    logs = rpc("eth_getLogs", [{"address": h.NADFUN_ADDRS, "fromBlock": hex(lo), "toBlock": hex(hi)}])

    create_log = graduate_log = None
    for log in logs:
        topics = [str(t).lower() for t in log.get("topics", [])]
        if not topics:
            continue
        if topics[0] in CREATE_TOPICS and len(topics) > 2 and topic_addr(topics[2]) == token:
            create_log = log
        elif topics[0] in GRADUATE_TOPICS and len(topics) > 1 and topic_addr(topics[1]) == token:
            graduate_log = log
    return create_log, graduate_log, approx


def plan_for_token(token: str) -> dict | None:
    info = nadfun_token_info(token)
    if not info:
        return None
    created_at = int(info.get("created_at") or 0)
    if not created_at:
        print(f"  ! {token}: nad.fun has no created_at")
        return None

    create_log, graduate_log, approx = find_creation(token, created_at)
    if create_log is None:
        print(f"  ! {token}: no TokenCreated log within [{approx - WINDOW_BEHIND}, {approx + WINDOW_AHEAD}]")
        return None

    emitter = str(create_log["address"]).lower()
    source = nadfun_geo.SOURCE_V2 if emitter == h.NADFUN_V2_ADDR else nadfun_geo.SOURCE_V1
    parsed = nadfun.parse_nadfun_token_created(
        emitter, [str(t) for t in create_log["topics"]], str(create_log["data"])[2:]
    )
    block = int(create_log["blockNumber"], 16)

    quote_token = (parsed.get("quote_token") or "").lower()
    graduate_pool = (
        topic_addr(str(graduate_log["topics"][2]))
        if graduate_log is not None and len(graduate_log.get("topics", [])) > 2
        else ""
    )
    graduate_block = int(graduate_log["blockNumber"], 16) if graduate_log is not None else 0
    pool_source = "log" if graduate_pool else ""

    # the graduation usually happens long after creation and is far outside the
    # window, so fall back to asking the factory for the pair
    if not graduate_pool and info.get("is_graduated"):
        graduate_pool = pair_for(token, quote_token)
        if graduate_pool:
            pool_source = "factory"

    return {
        "token": token,
        "source": source,
        "emitter": emitter,
        "block": block,
        "timestamp": block_timestamp(block),
        "creator": parsed.get("creator", ""),
        "name": parsed.get("name", ""),
        "symbol": parsed.get("symbol", ""),
        "token_uri": parsed.get("token_uri", ""),
        "quote_token": quote_token,
        "graduated": bool(info.get("is_graduated")),
        "graduate_pool": graduate_pool,
        "graduate_block": graduate_block,
        "pool_source": pool_source,
    }


def apply_plan(plan: dict) -> None:
    token = plan["token"]
    with db_cursor() as cur:
        storage.upsert_token_created(
            token=token,
            creator=plan["creator"],
            name=plan["name"],
            symbol=plan["symbol"],
            metadata_cid="",
            description="",
            social1="",
            social2="",
            social3="",
            social4="",
            source=plan["source"],
            created_block=plan["block"],
            created_at=plan["timestamp"],
            last_price_native=0,
            quote_token=plan["quote_token"] or None,
            cur=cur,
        )
        if plan["source"] == nadfun_geo.SOURCE_V2:
            storage.mark_nadfun_v2(token, cur=cur)
        if plan["token_uri"]:
            # the indexer's metadata sweep only looks at tokens that have a uri,
            # so this is what makes the image and socials fill in by themselves
            storage.set_token_uri(token, plan["token_uri"], cur=cur)

        if plan["graduate_pool"]:
            quote = plan["quote_token"]
            if quote and quote != token:
                storage.upsert_pool(
                    pool=plan["graduate_pool"],
                    token_addr=token,
                    native_addr=quote,
                    token_is_0=pair_token0(plan["graduate_pool"]) == token,
                    cur=cur,
                )
            # only the log gives a real graduation height. when the pair came
            # from the factory the creation block is a stand in, so the token
            # reads as graduated without claiming a height it never had
            grad_block = plan["graduate_block"] or plan["block"]
            storage.mark_token_migrated(
                token=token,
                migrated_block=grad_block,
                migrated_at=block_timestamp(grad_block) if plan["graduate_block"] else plan["timestamp"],
                pool=plan["graduate_pool"],
                cur=cur,
            )
        elif plan["graduated"]:
            # nad.fun says it graduated but the log is outside the window, so the
            # pool stays unknown. flagging it migrated is still better than
            # showing a graduated token as an active curve with empty reserves
            storage.mark_token_migrated(
                token=token,
                migrated_block=plan["block"],
                migrated_at=plan["timestamp"],
                pool=None,
                cur=cur,
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tokens", default="", help="comma separated token addresses")
    ap.add_argument("--discover", action="store_true", help="scan nad.fun listings for missing tokens")
    ap.add_argument("--pages", type=int, default=4, help="listing pages per feed when discovering")
    ap.add_argument("--limit", type=int, default=30, help="entries per listing page")
    ap.add_argument("--check", choices=("db", "api"), default="db", help="where to test whether a token is missing")
    ap.add_argument("--apply", action="store_true", help="write. without this it only reports")
    ap.add_argument("--i-know-the-host", action="store_true", help="allow --apply against a non-r3 host")
    args = ap.parse_args()

    preflight(args.apply, args.i_know_the_host)

    candidates = [t.strip().lower() for t in args.tokens.split(",") if t.strip()]
    if args.discover:
        print("discovering nad.fun tokens...")
        candidates.extend(t for t in discover_candidates(args.pages, args.limit) if t not in candidates)
    if not candidates:
        ap.error("pass --tokens or --discover")

    is_present = present_in_db if args.check == "db" else present_in_api
    print(f"checking {len(candidates)} tokens against {args.check}...")
    missing = [t for t in candidates if not is_present(t)]
    print(f"{len(missing)} missing\n")

    plans = []
    for token in missing:
        print(f"resolving {token}")
        plan = plan_for_token(token)
        if plan:
            plans.append(plan)
            if plan["graduate_pool"]:
                where = f"@{plan['graduate_block']}" if plan["graduate_block"] else " (height approx)"
                grad = f" graduated{where} pool={plan['graduate_pool']} [{plan['pool_source']}]"
            else:
                grad = " graduated (POOL UNKNOWN - trades will keep dropping)" if plan["graduated"] else ""
            print(f"  {plan['symbol']!r} {plan['name']!r} source={plan['source']} "
                  f"created@{plan['block']} creator={plan['creator']}{grad}")

    if not plans:
        print("\nnothing to repair")
        return 0

    if not args.apply:
        print(f"\n{len(plans)} token(s) would be written. re-run with --apply")
        return 0

    init_pool()
    for plan in plans:
        apply_plan(plan)
        print(f"wrote {plan['token']} ({plan['symbol']})")
    print(f"\nrepaired {len(plans)} token(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
