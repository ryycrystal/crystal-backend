from __future__ import annotations


def _clean_str(s: str) -> str:
    return (s or "").replace("\x00", "")


_ZERO_ADDR = "0x" + "0" * 40


def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]


def chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))


def _u256_at(data: bytes, off: int) -> int:
    if off < 0 or len(data) < off + 32:
        return 0
    return int.from_bytes(data[off : off + 32], "big")


def _addr_at(data: bytes, off: int) -> str:
    if off < 0 or len(data) < off + 32:
        return _ZERO_ADDR
    return "0x" + data[off + 12 : off + 32].hex().lower()


def _decode_dyn_str(data: bytes, base: int, rel: int) -> str:
    ptr = base + int(rel or 0)
    if ptr < 0 or len(data) < ptr + 32:
        return ""
    s_len = _u256_at(data, ptr)
    start = ptr + 32
    end = start + s_len
    if s_len < 0 or end > len(data):
        return ""
    try:
        return _clean_str(data[start:end].decode("utf-8", errors="ignore"))
    except Exception:
        return ""


def _decode_token_meta(data: bytes, base: int) -> tuple[str, int, str, str]:
    token = _addr_at(data, base + 0)
    decimals = _u256_at(data, base + 32)
    ticker_rel = _u256_at(data, base + 64)
    name_rel = _u256_at(data, base + 96)
    ticker = _decode_dyn_str(data, base, ticker_rel)
    name = _decode_dyn_str(data, base, name_rel)
    return token, decimals, ticker, name


MARKET_CREATED_V2_TOPIC = "0x6535cb10b05d5c7e575df57a5cc8af1760fbb9a159730e9379460f907816a8ba"
MARKET_PARAMS_CHANGED_V2_TOPIC = "0x6707271370cfc32da2a633047d48e0840473aa0a5477c1b19f151f12cd02ed85"


def parse_market_created(addr: str, tops: list[str], data_no0x: str):
    is_canonical = False
    if len(tops) > 1:
        try:
            is_canonical = int(tops[1], 16) != 0
        except Exception:
            is_canonical = False

    quote_asset = to_addr(tops[2]) if len(tops) > 2 else _ZERO_ADDR
    base_asset = to_addr(tops[3]) if len(tops) > 3 else _ZERO_ADDR

    data = bytes.fromhex(data_no0x)

    v2 = bool(tops) and str(tops[0]).lower() == MARKET_CREATED_V2_TOPIC
    market_addr = _addr_at(data, 0)
    creator = _addr_at(data, 32) if v2 else _ZERO_ADDR
    quote_info_off = _u256_at(data, 64 if v2 else 32)
    base_info_off = _u256_at(data, 96 if v2 else 64)
    pos_details = 128 if v2 else 96

    quote_address, quote_decimals, quote_ticker, quote_name = _decode_token_meta(data, quote_info_off)
    base_address, base_decimals, base_ticker, base_name = _decode_token_meta(data, base_info_off)
    if quote_address == _ZERO_ADDR or quote_address.lower() != quote_asset.lower():
        quote_address = quote_asset.lower()
    if base_address == _ZERO_ADDR or base_address.lower() != base_asset.lower():
        base_address = base_asset.lower()

    market_id = _u256_at(data, pos_details + 32 * 0)
    market_type = _u256_at(data, pos_details + 32 * 1)
    scale_factor = _u256_at(data, pos_details + 32 * 2)
    tick_size = _u256_at(data, pos_details + 32 * 3)
    max_price = _u256_at(data, pos_details + 32 * 4)
    min_size = _u256_at(data, pos_details + 32 * 5)
    taker_fee = _u256_at(data, pos_details + 32 * 6)
    maker_rebate = _u256_at(data, pos_details + 32 * 7)

    return {
        "isCanonical": is_canonical,
        "creator": creator.lower(),
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

    is_buy = u(0) != 0
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


def parse_market_params_changed(addr: str, tops: list[str], data_no0x: str) -> dict:
    market = to_addr(tops[1]).lower() if len(tops) > 1 else addr.lower()
    words = list(chunks(data_no0x, 64))

    def u(i: int, d: int = 0) -> int:
        return int(words[i], 16) if i < len(words) else d

    if bool(tops) and str(tops[0]).lower() == MARKET_PARAMS_CHANGED_V2_TOPIC:
        return {
            "market": market,
            "creator": "0x" + words[0][-40:] if words else _ZERO_ADDR,
            "minSize": u(1),
            "takerFee": u(2),
            "makerRebate": u(3),
            "creatorFee": u(4),
            "isAMMEnabled": bool(u(5)),
            "isCanonical": bool(u(6)),
        }

    return {
        "market": market,
        "minSize": u(0),
        "takerFee": u(1),
        "makerRebate": u(2),
        "isAMMEnabled": bool(u(3)),
    }
