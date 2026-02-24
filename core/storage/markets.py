from __future__ import annotations
from typing import Optional
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json, execute_values

from .base import db_cursor, _clean_text

def upsert_crystal_market(
    *,
    market: str,
    is_canonical: bool,
    quote_asset: str,
    base_asset: str,
    quote_address: str,
    quote_decimals: int,
    quote_ticker: str,
    quote_name: str,
    base_address: str,
    base_decimals: int,
    base_ticker: str,
    base_name: str,
    market_id: int,
    market_type: int,
    scale_factor: int,
    tick_size: int,
    max_price: int,
    min_size: int,
    taker_fee: int,
    maker_rebate: int,
    is_amm_enabled: bool,
    created_block: int | None,
    created_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        market.lower(),
        bool(is_canonical),
        quote_asset.lower(),
        base_asset.lower(),
        quote_address.lower(),
        int(quote_decimals),
        _clean_text(quote_ticker),
        _clean_text(quote_name),
        base_address.lower(),
        int(base_decimals),
        _clean_text(base_ticker),
        _clean_text(base_name),
        int(market_id or 0),
        int(market_type or 0),
        int(scale_factor or 0),
        int(tick_size or 0),
        int(max_price or 0),
        int(min_size or 0),
        int(taker_fee or 0),
        int(maker_rebate or 0),
        bool(is_amm_enabled),
        int(created_block) if created_block is not None else None,
        int(created_at) if created_at is not None else None,
        int(created_block) if created_block is not None else None,
        int(created_at) if created_at is not None else None,
    )
    sql = """
        INSERT INTO crystal_markets (
            market, is_canonical, quote_asset, base_asset, quote_address, quote_decimals,
            quote_ticker, quote_name, base_address, base_decimals, base_ticker, base_name,
            market_id, market_type, scale_factor, tick_size, max_price, min_size, taker_fee, maker_rebate, is_amm_enabled,
            created_block, created_at, updated_block, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market) DO UPDATE SET
            is_canonical = EXCLUDED.is_canonical,
            quote_asset = EXCLUDED.quote_asset,
            base_asset = EXCLUDED.base_asset,
            quote_address = EXCLUDED.quote_address,
            quote_decimals = EXCLUDED.quote_decimals,
            quote_ticker = EXCLUDED.quote_ticker,
            quote_name = EXCLUDED.quote_name,
            base_address = EXCLUDED.base_address,
            base_decimals = EXCLUDED.base_decimals,
            base_ticker = EXCLUDED.base_ticker,
            base_name = EXCLUDED.base_name,
            market_id = EXCLUDED.market_id,
            market_type = EXCLUDED.market_type,
            scale_factor = EXCLUDED.scale_factor,
            tick_size = EXCLUDED.tick_size,
            max_price = EXCLUDED.max_price,
            min_size = EXCLUDED.min_size,
            taker_fee = EXCLUDED.taker_fee,
            maker_rebate = EXCLUDED.maker_rebate,
            is_amm_enabled = EXCLUDED.is_amm_enabled,
            updated_block = COALESCE(EXCLUDED.updated_block, crystal_markets.updated_block),
            updated_at = COALESCE(EXCLUDED.updated_at, crystal_markets.updated_at);
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def update_crystal_market_price(
    market: str,
    last_price,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        Decimal(last_price),
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        market.lower(),
    )
    sql = """
        UPDATE crystal_markets
        SET last_price = %s,
            updated_block = COALESCE(%s, updated_block),
            updated_at = COALESCE(%s, updated_at)
        WHERE market = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def update_crystal_market_params(
    market: str,
    min_size: int,
    taker_fee: int,
    maker_rebate: int,
    is_amm_enabled: bool,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        int(min_size or 0),
        int(taker_fee or 0),
        int(maker_rebate or 0),
        bool(is_amm_enabled),
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        (market or "").lower(),
    )
    sql = """
        UPDATE crystal_markets
        SET min_size = %s,
            taker_fee = %s,
            maker_rebate = %s,
            is_amm_enabled = %s,
            updated_block = COALESCE(%s, updated_block),
            updated_at = COALESCE(%s, updated_at)
        WHERE market = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def link_crystal_vaults_for_market(
    *,
    quote: str,
    base: str,
    market: str,
    quote_decimals: int,
    base_decimals: int,
    updated_block: int | None,
    updated_at: int | None,
    cur: psycopg2.extensions.cursor | None = None,
) -> None:
    params = (
        market.lower(),
        int(quote_decimals or 0),
        int(base_decimals or 0),
        int(updated_block) if updated_block is not None else None,
        int(updated_at) if updated_at is not None else None,
        quote.lower(),
        base.lower(),
    )
    sql = """
        UPDATE crystal_vaults
        SET market = CASE WHEN market = '' THEN %s ELSE market END,
            quote_decimals = CASE WHEN quote_decimals = 0 THEN %s ELSE quote_decimals END,
            base_decimals = CASE WHEN base_decimals = 0 THEN %s ELSE base_decimals END,
            updated_block = COALESCE(%s, updated_block),
            updated_at = COALESCE(%s, updated_at)
        WHERE quote = %s AND base = %s;
    """
    if cur is None:
        with db_cursor() as cur2:
            cur2.execute(sql, params)
    else:
        cur.execute(sql, params)


def load_crystal_markets_for_state():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market, is_canonical, quote_asset, base_asset, quote_address, quote_decimals,
                quote_ticker, quote_name, base_address, base_decimals, base_ticker, base_name,
                market_id, market_type, scale_factor, tick_size, max_price, min_size, taker_fee, maker_rebate,
                is_amm_enabled, last_price
            FROM crystal_markets
            """
        )
        return cur.fetchall()


def list_crystal_markets_dump():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
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
                updated_at
            FROM crystal_markets
            ORDER BY COALESCE(updated_at, created_at, 0) DESC, market ASC
            """
        )
        return cur.fetchall()


def list_crystal_pool_markets():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market,
                quote_address,
                base_address,
                market_type,
                quote_decimals,
                base_decimals,
                quote_ticker,
                quote_name,
                base_ticker,
                base_name,
                taker_fee,
                is_amm_enabled,
                last_price,
                updated_at,
                created_at
            FROM crystal_markets
            WHERE is_canonical = TRUE
              AND market_type > 1
              AND is_amm_enabled = TRUE
            ORDER BY COALESCE(updated_at, created_at, 0) DESC, market ASC
            """
        )
        return cur.fetchall()


def get_crystal_pool_market(market: str):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
                market,
                quote_address,
                base_address,
                market_type,
                quote_decimals,
                base_decimals,
                quote_ticker,
                quote_name,
                base_ticker,
                base_name,
                taker_fee,
                is_amm_enabled,
                last_price,
                updated_at,
                created_at
            FROM crystal_markets
            WHERE is_canonical = TRUE
              AND market_type > 1
              AND is_amm_enabled = TRUE
              AND market = %s
            LIMIT 1
            """,
            ((market or "").lower(),),
        )
        return cur.fetchone()


