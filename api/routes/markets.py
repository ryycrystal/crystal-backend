from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from fastapi import APIRouter

import core.storage as storage

router = APIRouter()


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


def _crystal_market_dump_row_to_api(row: Sequence[Any]) -> dict[str, Any]:
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


@router.get("/markets/list")
def list_markets_dump() -> dict[str, Any]:
    from api.api import _mon_price_usd

    rows = storage.list_crystal_markets_dump()
    stats = storage.crystal_market_stats_24h()
    # a graduated launchpad token keeps its art on the launchpad row, and the
    # market list is the only place the spot token picker learns about it
    images = storage.token_images_by_address()
    mon_price = _mon_price_usd()
    markets = []
    for row in rows:
        market = _crystal_market_dump_row_to_api(row)
        market["baseImage"] = images.get((market["base"]["address"] or "").lower(), "")
        market["quoteImage"] = images.get((market["quote"]["address"] or "").lower(), "")
        market["stats24h"] = stats.get(
            market["market"],
            {
                "openPrice": "0",
                "highPrice": "0",
                "lowPrice": "0",
                "lastPrice": str(market["params"]["lastPrice"] or 0),
                "quoteVolume": "0",
                "trades": [],
            },
        )
        if market["isCanonical"]:
            market["klines"] = storage.market_klines(market["market"], 3600, 24)
            market["trades"] = storage.list_market_recent_trades(market["market"], 50)
        else:
            market["klines"] = []
            market["trades"] = []
        qv = Decimal(market["stats24h"].get("quoteVolume") or 0)
        if market["quote"]["ticker"] == "USDC":
            usd = qv / Decimal(10) ** 6
        else:
            usd = qv / Decimal(10) ** int(market["quote"]["decimals"] or 18) * mon_price
        market["usdVolume24h"] = str(usd)
        markets.append(market)
    return {"count": len(markets), "markets": markets}


@router.get("/price/mon")
def mon_price() -> dict[str, Any]:
    from api.api import _mon_price_usd

    return {"usd": float(_mon_price_usd())}
