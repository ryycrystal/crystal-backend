import argparse
import asyncio
import time

import backfill
import core.storage as storage
from core import chain as h
from core.sequencer import SEQUENCER

VAULT_APPLY = {
    "VD": "apply_vault_deployed",
    "VDP": "apply_vault_deposit",
    "VWD": "apply_vault_withdraw",
    "VLOCK": "apply_vault_locked",
    "VUNLOCK": "apply_vault_unlocked",
    "VCLOSE": "apply_vault_closed",
    "VMAX": "apply_vault_max_shares_changed",
    "VLOCKUP": "apply_vault_lockup_changed",
    "VDECR": "apply_vault_decrease_on_withdraw_changed",
}


async def _logs_for(address: str, frm: int, to: int) -> list[dict]:
    data = await backfill.http_jsonrpc(
        "eth_getLogs",
        [{"fromBlock": hex(frm), "toBlock": hex(to), "address": address}],
    )
    return data.get("result") or []


def _apply(log: dict, tag: str, parsed: dict, cur) -> bool:
    blk = int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else int(log["blockNumber"])
    raw_ts = log.get("blockTimestamp")
    ts = int(raw_ts, 16) if isinstance(raw_ts, str) else int(raw_ts or 0)
    li = int(log["logIndex"], 16) if isinstance(log["logIndex"], str) else int(log["logIndex"] or 0)
    txh = (log.get("transactionHash") or "").lower()
    addr = (log.get("address") or "").lower()

    method = getattr(SEQUENCER._state, VAULT_APPLY[tag])
    if tag in ("VDP", "VWD"):
        method(blk, ts, txh, parsed, addr, cur=cur, log_idx=li)
    else:
        method(blk, ts, parsed, addr, cur=cur)
    return True


async def run(address: str, start_block: int, end_block: int, chunk: int, dry_run: bool) -> None:
    address = address.lower()
    print(f"[VAULTFILL] scanning {address} across {start_block:,}-{end_block:,}", flush=True)

    SEQUENCER._state.rebuild_from_db()
    applied: dict[str, int] = {}
    seen = 0
    blk = start_block
    size = chunk

    while blk <= end_block:
        to = min(blk + size - 1, end_block)
        try:
            logs = await _logs_for(address, blk, to)
        except Exception as e:
            if size > 100:
                size = max(100, size // 10)
                print(f"[VAULTFILL] range {blk}-{to} failed ({e!r}), shrinking to {size}", flush=True)
                continue
            print(f"[VAULTFILL] skipping {blk}-{to}: {e!r}", flush=True)
            blk = to + 1
            continue

        for log in logs:
            topics = log.get("topics") or []
            if not topics:
                continue
            tag = h.EVENT_SIGS.get(str(topics[0]).lower())
            if tag not in VAULT_APPLY:
                continue
            parser = h.PARSERS.get(tag)
            parsed = parser(str(log.get("address", "")).lower(), topics, str(log.get("data", "0x"))[2:])
            if not parsed:
                continue
            seen += 1
            if dry_run:
                applied[tag] = applied.get(tag, 0) + 1
                continue
            with storage.db_cursor() as cur:
                _apply(log, tag, parsed, cur)
            applied[tag] = applied.get(tag, 0) + 1

        blk = to + 1

    verb = "would apply" if dry_run else "applied"
    print(f"[VAULTFILL] {verb} {seen} vault events: {applied or '{}'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="reindex one vault factory's events straight from rpc")
    parser.add_argument("--address", required=True)
    parser.add_argument("--start-block", type=lambda x: int(x, 0), required=True)
    parser.add_argument("--end-block", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--chunk", type=int, default=2000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    storage.init_pool()
    end = args.end_block or int(storage.get_last_processed_block() or 0)
    t0 = time.time()
    asyncio.run(run(args.address, args.start_block, end, args.chunk, args.dry_run))
    print(f"[VAULTFILL] done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
