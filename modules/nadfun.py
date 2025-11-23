from __future__ import annotations

import json
from decimal import Decimal
from typing import Dict, Optional
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

# pending nadfun sync snapshots keyed by token address bc they emit it seperately
_PENDING_SYNC: Dict[str, dict] = {}

# 32-byte word or hex string into a 0x-prefixed address
def _to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

# read the 32-byte word at 'index' from a hex string (no 0x) interpreted as uint256
def _word(data_hex: str, index: int) -> int:
    if not data_hex:
        return 0
    start = index * 64
    end = start + 64
    if end > len(data_hex):
        return 0
    return int(data_hex[start:end], 16)

# # yield s in fixed-size n-character chunks (used for 32-byte words)
def _chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))

# decode an abi-encoded string from the calldata hex starting at 'word_index'
def _decode_string(data_hex: str, word_index: int) -> str:
    if not data_hex:
        return ""

    try:
        data = bytes.fromhex(data_hex)
    except ValueError:
        return ""

    base = word_index * 32
    if base + 32 > len(data):
        return ""

    offset = int.from_bytes(data[base : base + 32], "big")
    if offset + 32 > len(data):
        return ""

    length = int.from_bytes(data[offset : offset + 32], "big")
    start = offset + 32
    end = start + length
    if start > len(data):
        return ""
    if end > len(data):
        end = len(data)

    try:
        return data[start:end].decode("utf-8", errors="ignore")
    except Exception:
        return ""

# parse a hex string into a signed 256-bit integer (two's complement)
def _int256_from_hex(x: str) -> int:
    if x.startswith("0x"):
        x = x[2:]
    if not x:
        return 0
    n = int(x, 16)
    if n >= 2**255:
        n -= 2**256
    return n

# CurveCreate(
#   address creator,
#   address token,
#   address pool,
#   string name,
#   string symbol,
#   string tokenURI,
#   uint256 virtualMonReserve,
#   uint256 virtualTokenReserve,
#   uint256 targetTokenAmount
# );
# into a flat dict for state.apply_token_created
def parse_nadfun_token_created(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict:
    creator = _to_addr(topics[1]) if len(topics) > 1 else ""
    token = _to_addr(topics[2]) if len(topics) > 2 else ""

    name = _decode_string(data_no0x, 0)
    symbol = _decode_string(data_no0x, 1)
    token_uri = _decode_string(data_no0x, 2)

    description: str = ""
    image_uri: str = ""
    website: str = ""
    twitter: str = ""
    telegram: str = ""

    if token_uri:
        try:
            with urlopen(token_uri, timeout=5) as resp:
                raw = resp.read()
            meta = json.loads(raw.decode("utf-8"))

            name = meta.get("name", name) or name
            symbol = meta.get("symbol", symbol) or symbol
            description = meta.get("description", "") or ""
            image_uri = meta.get("image_uri", "") or ""
            website = meta.get("website", "") or ""
            twitter = meta.get("twitter", "") or ""
            telegram = meta.get("telegram", "") or ""
        except (URLError, HTTPError, ValueError, json.JSONDecodeError):
            image_uri = ""

    metadata_cid = image_uri or token_uri or ""

    try:
        last_price_native = Decimal("90000") / Decimal("1073000191")
    except Exception:
        last_price_native = Decimal(0)

    return {
        "token": token,
        "creator": creator,
        "name": name,
        "symbol": symbol,
        "metadata_cid": metadata_cid,
        "description": description,
        "social1": website,
        "social2": twitter,
        "social3": telegram,
        "social4": "",
        "source": 1,
        "last_price_native": last_price_native,
    }

# CurveSync(
#   address token,
#   uint256 realMonReserve,
#   uint256 realTokenReserve,
#   uint256 virtualMonReserve,
#   uint256 virtualTokenReserve
# );
# and stashes latest reserves to be used by the next buy/sell event for that token
def parse_nadfun_sync(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    if len(topics) < 2:
        return None

    token = _to_addr(topics[1])

    real_mon = _word(data_no0x, 0)
    real_token = _word(data_no0x, 1)
    virtual_mon = _word(data_no0x, 2)
    virtual_token = _word(data_no0x, 3)

    _PENDING_SYNC[token] = {
        "token": token,
        "real_native_reserve": real_mon,
        "real_token_reserve": real_token,
        "native_reserve": virtual_mon,
        "token_reserve": virtual_token,
    }

    return None

# pop and return the last sync snapshot for 'token' or zeros if none
def _consume_sync_for_token(token: str) -> dict:
    sync = _PENDING_SYNC.pop(token.lower(), None)
    if not sync:
        return {
            "native_reserve": 0,
            "token_reserve": 0,
            "real_native_reserve": 0,
            "real_token_reserve": 0,
        }

    return {
        "native_reserve": int(sync.get("native_reserve", 0)),
        "token_reserve": int(sync.get("token_reserve", 0)),
        "real_native_reserve": int(sync.get("real_native_reserve", 0)),
        "real_token_reserve": int(sync.get("real_token_reserve", 0)),
    }

# CurveBuy(
#   address to, 
#   address token, 
#   uint256 actualAmountIn, 
#   uint256 effectiveAmountOut
# );
# into a flat trade dict, merges in latest sync reserves, for state.apply_launchpad_trade
def parse_nadfun_buy(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    if len(topics) < 3:
        return None

    user = _to_addr(topics[1])
    token = _to_addr(topics[2])

    actual_in = _word(data_no0x, 0)
    effective_out = _word(data_no0x, 1)

    sync = _consume_sync_for_token(token)

    return {
        "token": token,
        "user": user,
        "is_buy": True,
        "amount_in": actual_in,
        "amount_out": effective_out,
        "native_reserve": sync["native_reserve"],
        "token_reserve": sync["token_reserve"],
    }

# CurveSell(
#   address to, 
#   address token, 
#   uint256 actualAmountIn, 
#   uint256 effectiveAmountOut
# );
# into a flat trade dict, merges in latest sync reserves, for state.apply_launchpad_trade
def parse_nadfun_sell(
    _addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    if len(topics) < 3:
        return None

    user = _to_addr(topics[1])
    token = _to_addr(topics[2])

    actual_in = _word(data_no0x, 0)
    effective_out = _word(data_no0x, 1)

    sync = _consume_sync_for_token(token)

    return {
        "token": token,
        "user": user,
        "is_buy": False,
        "amount_in": actual_in,
        "amount_out": effective_out,
        "native_reserve": sync["native_reserve"],
        "token_reserve": sync["token_reserve"],
    }

# CurveGraduate(token, pool);
# with a pool param for state.apply_migrated
def parse_nadfun_graduated(
    _addr: str,
    topics: list[str],
    _data_no0x: str,
) -> dict:
    token = _to_addr(topics[1]).lower() if len(topics) > 1 else ""
    pool = _to_addr(topics[2]).lower() if len(topics) > 2 else ""
    return {
        "token": token, 
        "pool": pool
    }

# Swap(
#   address sender,
#   address recipient,
#   int256 amount0,
#   int256 amount1,
#   uint160 sqrtPriceX96,
#   uint128 liquidity,
#   int24 tick
# );
# parses uniswap v3-style Swap event into a dict consumable by state.apply_launchpad_trade (slightly diff shape)
# amount0 and amount1 are signed deltas, sqrt_price_x96 is the new sqrt price
def parse_v3_trade(addr, tops, data):
    pool = addr.lower()
    sender = _to_addr(tops[1]).lower() if len(tops) > 1 else ""
    recipient = _to_addr(tops[2]).lower() if len(tops) > 2 else ""

    if isinstance(data, str) and data.startswith("0x"):
        hex_data = data[2:]
    else:
        hex_data = data

    words = list(_chunks(hex_data, 64)) 

    if len(words) < 5:
        return {
            "pool": pool,
            "sender": sender,
            "user": recipient,
            "amount0": 0,
            "amount1": 0,
            "sqrt_price_x96": 0,
        }

    amount0 = _int256_from_hex(words[0])
    amount1 = _int256_from_hex(words[1])

    try:
        sqrt_price_x96 = int(words[2], 16)
    except Exception:
        sqrt_price_x96 = 0

    return {
        "pool": pool,
        "sender": sender,
        "user": recipient,
        "amount0": amount0,
        "amount1": amount1,
        "sqrt_price_x96": sqrt_price_x96,
    }