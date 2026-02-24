from __future__ import annotations

from typing import Dict, Any

def _clean_str(s: str) -> str:
    return (s or "").replace("\x00", "")

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

def chunks(s: str, n: int):
    return (s[i:i+n] for i in range(0, len(s), n))

def parse_market_created(addr: str, tops: list[str], data_no0x: str):
    is_canonical = False
    if len(tops) > 1:
        try:
            is_canonical = int(tops[1], 16) != 0
        except Exception:
            is_canonical = False
            
    quote_asset = to_addr(tops[2]) if len(tops) > 2 else "0x" + "0" * 40
    base_asset = to_addr(tops[3]) if len(tops) > 3 else "0x" + "0" * 40

    data = bytes.fromhex(data_no0x)

    POS_MARKET = 0
    POS_QUOTE_HEAD = 32
    POS_BASE_HEAD = 160
    POS_DETAILS = 288

    market_addr = "0x" + data[POS_MARKET + 12: POS_MARKET + 32].hex().lower() if len(data) >= POS_MARKET + 32 else "0x" + "0" * 40

    quote_address = "0x" + data[POS_QUOTE_HEAD + 12: POS_QUOTE_HEAD + 32].hex().lower() if len(data) >= POS_QUOTE_HEAD + 32 else "0x" + "0" * 40
    quote_decimals = int.from_bytes(data[POS_QUOTE_HEAD + 32: POS_QUOTE_HEAD + 64], "big") if len(data) >= POS_QUOTE_HEAD + 64 else 0

    quote_ticker = ""
    if len(data) >= POS_QUOTE_HEAD + 96:
        qt_rel = int.from_bytes(data[POS_QUOTE_HEAD + 64: POS_QUOTE_HEAD + 96], "big")
        qt_ptr = POS_QUOTE_HEAD + qt_rel
        if len(data) >= qt_ptr + 32:
            qt_len = int.from_bytes(data[qt_ptr: qt_ptr + 32], "big")
            qt_start = qt_ptr + 32
            qt_end = qt_start + qt_len
            if 0 <= qt_len and qt_end <= len(data):
                try:
                    quote_ticker = _clean_str(data[qt_start: qt_end].decode("utf-8", errors="ignore"))
                except Exception:
                    quote_ticker = ""

    quote_name = ""
    if len(data) >= POS_QUOTE_HEAD + 128:
        qn_rel = int.from_bytes(data[POS_QUOTE_HEAD + 96: POS_QUOTE_HEAD + 128], "big")
        qn_ptr = POS_QUOTE_HEAD + qn_rel
        if len(data) >= qn_ptr + 32:
            qn_len = int.from_bytes(data[qn_ptr: qn_ptr + 32], "big")
            qn_start = qn_ptr + 32
            qn_end = qn_start + qn_len
            if 0 <= qn_len and qn_end <= len(data):
                try:
                    quote_name = _clean_str(data[qn_start: qn_end].decode("utf-8", errors="ignore"))
                except Exception:
                    quote_name = ""

    base_address = "0x" + data[POS_BASE_HEAD + 12: POS_BASE_HEAD + 32].hex().lower() if len(data) >= POS_BASE_HEAD + 32 else "0x" + "0" * 40
    base_decimals = int.from_bytes(data[POS_BASE_HEAD + 32: POS_BASE_HEAD + 64], "big") if len(data) >= POS_BASE_HEAD + 64 else 0

    base_ticker = ""
    if len(data) >= POS_BASE_HEAD + 96:
        bt_rel = int.from_bytes(data[POS_BASE_HEAD + 64: POS_BASE_HEAD + 96], "big")
        bt_ptr = POS_BASE_HEAD + bt_rel
        if len(data) >= bt_ptr + 32:
            bt_len = int.from_bytes(data[bt_ptr: bt_ptr + 32], "big")
            bt_start = bt_ptr + 32
            bt_end = bt_start + bt_len
            if 0 <= bt_len and bt_end <= len(data):
                try:
                    base_ticker = _clean_str(data[bt_start: bt_end].decode("utf-8", errors="ignore"))
                except Exception:
                    base_ticker = ""

    base_name = ""
    if len(data) >= POS_BASE_HEAD + 128:
        bn_rel = int.from_bytes(data[POS_BASE_HEAD + 96: POS_BASE_HEAD + 128], "big")
        bn_ptr = POS_BASE_HEAD + bn_rel
        if len(data) >= bn_ptr + 32:
            bn_len = int.from_bytes(data[bn_ptr: bn_ptr + 32], "big")
            bn_start = bn_ptr + 32
            bn_end = bn_start + bn_len
            if 0 <= bn_len and bn_end <= len(data):
                try:
                    base_name = _clean_str(data[bn_start: bn_end].decode("utf-8", errors="ignore"))
                except Exception:
                    base_name = ""

    market_id = int.from_bytes(data[POS_DETAILS + 32 * 0: POS_DETAILS + 32 * 1], "big") if len(data) >= POS_DETAILS + 32 * 1 else 0
    market_type = int.from_bytes(data[POS_DETAILS + 32 * 1: POS_DETAILS + 32 * 2], "big") if len(data) >= POS_DETAILS + 32 * 2 else 0
    scale_factor = int.from_bytes(data[POS_DETAILS + 32 * 2: POS_DETAILS + 32 * 3], "big") if len(data) >= POS_DETAILS + 32 * 3 else 0
    tick_size = int.from_bytes(data[POS_DETAILS + 32 * 3: POS_DETAILS + 32 * 4], "big") if len(data) >= POS_DETAILS + 32 * 4 else 0
    max_price = int.from_bytes(data[POS_DETAILS + 32 * 4: POS_DETAILS + 32 * 5], "big") if len(data) >= POS_DETAILS + 32 * 5 else 0
    min_size = int.from_bytes(data[POS_DETAILS + 32 * 5: POS_DETAILS + 32 * 6], "big") if len(data) >= POS_DETAILS + 32 * 6 else 0
    taker_fee = int.from_bytes(data[POS_DETAILS + 32 * 6: POS_DETAILS + 32 * 7], "big") if len(data) >= POS_DETAILS + 32 * 7 else 0
    maker_rebate = int.from_bytes(data[POS_DETAILS + 32 * 7: POS_DETAILS + 32 * 8], "big") if len(data) >= POS_DETAILS + 32 * 8 else 0

    return {
        "isCanonical": is_canonical,
        "quoteAsset": quote_asset.lower(),
        "baseAsset": base_asset.lower(),
        "market": market_addr.lower(),
        "quoteAddress": quote_address.lower(),
        "quoteDecimals": quote_decimals,
        "quoteTicker": quote_ticker,
        "quoteName": quote_name,
        "baseAddress": base_address.lower(),
        "baseDecimals": base_decimals,
        "baseTicker": base_ticker,
        "baseName": base_name,
        "marketId": market_id,
        "marketType": market_type,
        "scaleFactor": scale_factor,
        "tickSize": tick_size,
        "maxPrice": max_price,
        "minSize": min_size,
        "takerFee": taker_fee,
        "makerRebate": maker_rebate,
    }

def parse_trade(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]).lower() if len(tops) > 1 else addr.lower()
    user = to_addr(tops[2]).lower() if len(tops) > 2 else ""

    words = list(chunks(data_no0x, 64))
    
    def u(i: int, d: int = 0) -> int:
        return int(words[i], 16) if i < len(words) else d

    is_buy = (u(0) != 0)
    amount_in = u(1)
    amount_out = u(2)
    start_px = u(3)
    end_px = u(4)

    return {
        "market": market,
        "user": user,
        "is_buy": is_buy,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "start_price": start_px,
        "end_price": end_px,
    }
