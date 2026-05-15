from __future__ import annotations
from decimal import Decimal
from typing import Dict, Any, Sequence

from fastapi import APIRouter
import core.storage as storage

router = APIRouter()


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


def _crystal_market_dump_row_to_api(row: Sequence[Any]) -> Dict[str, Any]:
    (
        market,
        is_canonical,
        quote_asset,
        base_asset,
        quote_address,
        quote_decimals,
        quote_ticker,
        quote_name,
        base_address,
        base_decimals,
        base_ticker,
        base_name,
        market_id,
        market_type,
        scale_factor,
        tick_size,
        max_price,
        min_size,
        taker_fee,
        maker_rebate,
        is_amm_enabled,
        last_price,
        created_block,
        created_at,
        updated_block,
        updated_at,
    ) = row
    return {
        "market": market,
        "isCanonical": bool(is_canonical),
        "quoteAsset": quote_asset,
        "baseAsset": base_asset,
        "quote": {
            "address": quote_address,
            "decimals": quote_decimals,
            "ticker": quote_ticker,
            "name": quote_name,
        },
        "base": {
            "address": base_address,
            "decimals": base_decimals,
            "ticker": base_ticker,
            "name": base_name,
        },
        "params": {
            "marketId": market_id,
            "marketType": market_type,
            "scaleFactor": scale_factor,
            "tickSize": tick_size,
            "maxPrice": max_price,
            "minSize": min_size,
            "takerFee": taker_fee,
            "makerRebate": maker_rebate,
            "isAmmEnabled": bool(is_amm_enabled),
            "lastPrice": _json_value(last_price),
        },
        "createdBlock": created_block,
        "createdAt": created_at,
        "updatedBlock": updated_block,
        "updatedAt": updated_at,
    }


# Return a full dump of indexed Crystal markets for inspection and debugging
@router.get("/markets/list")
def list_markets_dump() -> Dict[str, Any]:
    rows = storage.list_crystal_markets_dump()
    markets = [_crystal_market_dump_row_to_api(r) for r in rows]
    return {"count": len(markets), "markets": markets}
