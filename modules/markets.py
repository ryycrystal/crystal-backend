from __future__ import annotations
from eth_abi import decode as abi_decode

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

    data = bytes.fromhex(data_no0x) if data_no0x else b""

    market_addr = "0x" + "0" * 40
    quote_info = ("0x" + "0" * 40, 0, "", "")
    base_info = ("0x" + "0" * 40, 0, "", "")
    mdet = (0,) * 8

    if data:
        try:
            decoded = abi_decode(
                [
                    "address",
                    "(address,uint256,string,string)",
                    "(address,uint256,string,string)",
                    "(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)",
                ],
                data,
            )
            market_addr, quote_info, base_info, mdet = decoded
        except Exception:
            pass

    quote_address, quote_decimals, quote_ticker, quote_name = quote_info
    base_address, base_decimals, base_ticker, base_name = base_info
    (
        market_id,
        market_type,
        scale_factor,
        tick_size,
        max_price,
        min_size,
        taker_fee,
        maker_rebate,
    ) = mdet

    if isinstance(market_addr, bytes):
        market_addr_str = "0x" + market_addr.hex()
    else:
        market_addr_str = str(market_addr)

    if isinstance(quote_address, bytes):
        quote_address_str = "0x" + quote_address.hex()
    else:
        quote_address_str = str(quote_address)

    if isinstance(base_address, bytes):
        base_address_str = "0x" + base_address.hex()
    else:
        base_address_str = str(base_address)

    if quote_ticker is None:
        quote_ticker = ""
    if quote_name is None:
        quote_name = ""
    if base_ticker is None:
        base_ticker = ""
    if base_name is None:
        base_name = ""

    return {
        "isCanonical": is_canonical,
        "quoteAsset": quote_asset.lower(),
        "baseAsset": base_asset.lower(),
        "market": market_addr_str.lower(),
        "quoteAddress": quote_address_str.lower(),
        "quoteDecimals": int(quote_decimals),
        "quoteTicker": quote_ticker,
        "quoteName": quote_name,
        "baseAddress": base_address_str.lower(),
        "baseDecimals": int(base_decimals),
        "baseTicker": base_ticker,
        "baseName": base_name,
        "marketId": int(market_id),
        "marketType": int(market_type),
        "scaleFactor": int(scale_factor),
        "tickSize": int(tick_size),
        "maxPrice": int(max_price),
        "minSize": int(min_size),
        "takerFee": int(taker_fee),
        "makerRebate": int(maker_rebate),
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