from __future__ import annotations

import os
from decimal import Decimal, localcontext
from functools import lru_cache

from Crypto.Hash import keccak
from fastapi import APIRouter, HTTPException, Query

from api.api import db_cursor
from core.adapters.native import INITIAL_TOKEN_SUPPLY

router = APIRouter(prefix="/dexscreener")

DEX_KEY = os.getenv("DEXSCREENER_DEX_KEY", "crystal")
FEE_BPS = int(os.getenv("DEXSCREENER_FEE_BPS", "100"))
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
WAD = Decimal(10) ** 18
QUANTUM = Decimal(1).scaleb(-50)

CURVE_TRADE_FILTER = """
    k.source = 0
    AND (NOT k.migrated OR t.block_number < k.migrated_block
         OR (t.block_number = k.migrated_block AND t.native_reserve > 0))
"""


@lru_cache(maxsize=8192)
def _checksum(addr: str) -> str:
    a = addr.lower().removeprefix("0x")
    digest = keccak.new(digest_bits=256, data=a.encode()).hexdigest()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(a))


def _addr_or_404(v: str) -> str:
    a = (v or "").strip().lower()
    if a.startswith("0x") and len(a) == 42:
        try:
            int(a[2:], 16)
            return a
        except ValueError:
            pass
    raise HTTPException(status_code=404, detail="not found")


def _dec_str(raw) -> str:
    with localcontext() as ctx:
        ctx.prec = 160
        return format((Decimal(int(raw or 0)) / WAD).quantize(QUANTUM).normalize(), "f")


def _price_native(native_raw: int, token_raw: int, native_reserve: int, token_reserve: int) -> str:
    with localcontext() as ctx:
        ctx.prec = 160
        p = Decimal(native_raw) / Decimal(token_raw) if token_raw > 0 else Decimal(0)
        if p <= 0 and token_reserve > 0:
            p = Decimal(native_reserve) / Decimal(token_reserve)
        p = p.quantize(QUANTUM)
        if p <= 0:
            p = QUANTUM
        return format(p.normalize(), "f")


@router.get("/latest-block")
def dex_latest_block():
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM launchpad_kv WHERE key IN ('dex_tip_block', 'dex_tip_ts')")
        meta = dict(cur.fetchall())
        block_number = int(meta.get("dex_tip_block") or 0)
        block_ts = int(meta.get("dex_tip_ts") or 0)
        if not block_number or not block_ts:
            cur.execute("SELECT MAX(number) FROM launchpad_blocks")
            row = cur.fetchone()
            block_number = int(row[0]) if row and row[0] is not None else 0
            cur.execute(
                """
                SELECT timestamp FROM launchpad_trades
                WHERE block_number <= %s
                ORDER BY block_number DESC, log_index DESC
                LIMIT 1
                """,
                (block_number,),
            )
            trow = cur.fetchone()
            block_ts = int(trow[0]) if trow else 0
    if not block_number or not block_ts:
        raise HTTPException(status_code=503, detail="indexer checkpoint unavailable")
    return {"block": {"blockNumber": block_number, "blockTimestamp": block_ts}}


@router.get("/asset")
def dex_asset(id: str = Query(...)):
    addr = _addr_or_404(id)
    with db_cursor() as cur:
        cur.execute(
            "SELECT name, symbol, circulating_supply FROM launchpad_tokens WHERE token = %s AND source = 0",
            (addr,),
        )
        row = cur.fetchone()
        is_quote = False
        if row is None:
            cur.execute("SELECT 1 FROM launchpad_tokens WHERE quote_token = %s AND source = 0 LIMIT 1", (addr,))
            is_quote = cur.fetchone() is not None
    checksummed = _checksum(addr)
    if row is None:
        if not is_quote:
            raise HTTPException(status_code=404, detail="asset not found")
        if addr == WMON:
            return {"asset": {"id": checksummed, "name": "Wrapped Monad", "symbol": "WMON"}}
        return {"asset": {"id": checksummed, "name": checksummed, "symbol": checksummed[2:8].upper()}}
    name, symbol, circulating = row
    return {
        "asset": {
            "id": checksummed,
            "name": name or checksummed,
            "symbol": symbol or checksummed[2:8].upper(),
            "totalSupply": _dec_str(INITIAL_TOKEN_SUPPLY),
            "circulatingSupply": _dec_str(circulating),
        }
    }


@router.get("/pair")
def dex_pair(id: str = Query(...)):
    addr = _addr_or_404(id)
    with db_cursor() as cur:
        cur.execute(
            "SELECT creator, created_block, created_at, quote_token FROM launchpad_tokens WHERE token = %s AND source = 0",
            (addr,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="pair not found")
    creator, created_block, created_at, quote_token = row
    checksummed = _checksum(addr)
    pair = {
        "id": checksummed,
        "dexKey": DEX_KEY,
        "asset0Id": checksummed,
        "asset1Id": _checksum(quote_token or WMON),
        "createdAtBlockNumber": int(created_block),
        "createdAtBlockTimestamp": int(created_at),
        "feeBps": FEE_BPS,
    }
    if creator:
        pair["creator"] = _checksum(creator)
    return {"pair": pair}


@router.get("/events")
def dex_events(fromBlock: int = Query(...), toBlock: int = Query(...)):
    if toBlock < fromBlock:
        return {"events": []}
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT t.block_number, t.timestamp, t.txhash, t.log_index, t.user_address, t.token,
                   t.is_buy, t.native_amount, t.token_amount, t.native_reserve, t.token_reserve
            FROM launchpad_trades t
            JOIN launchpad_tokens k ON k.token = t.token
            WHERE t.block_number BETWEEN %s AND %s AND {CURVE_TRADE_FILTER}
            ORDER BY t.block_number, t.log_index
            """,
            (fromBlock, toBlock),
        )
        rows = cur.fetchall()
    events = []
    for block_number, ts, txhash, log_index, user, token, is_buy, native_amt, token_amt, native_res, token_res in rows:
        event = {
            "block": {"blockNumber": int(block_number), "blockTimestamp": int(ts)},
            "eventType": "swap",
            "txnId": txhash,
            "txnIndex": 0,
            "eventIndex": int(log_index),
            "maker": _checksum(user),
            "pairId": _checksum(token),
            "priceNative": _price_native(int(native_amt), int(token_amt), int(native_res or 0), int(token_res or 0)),
            "reserves": {"asset0": _dec_str(token_res), "asset1": _dec_str(native_res)},
        }
        if is_buy:
            event["asset1In"] = _dec_str(native_amt)
            event["asset0Out"] = _dec_str(token_amt)
        else:
            event["asset0In"] = _dec_str(token_amt)
            event["asset1Out"] = _dec_str(native_amt)
        events.append(event)
    return {"events": events}
