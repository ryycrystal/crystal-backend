from __future__ import annotations

import json
from decimal import Decimal
from typing import Dict, Optional
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

_PENDING_SYNC: Dict[str, dict] = {}


def _to_addr(topic: str) -> str:
    if not topic:
        return ""
    t = topic.lower()
    if t.startswith("0x"):
        t = t[2:]
    if len(t) < 40:
        return "0x" + t.rjust(40, "0")
    return "0x" + t[-40:]


def _word(data_hex: str, index: int) -> int:
    if not data_hex:
        return 0
    start = index * 64
    end = start + 64
    if end > len(data_hex):
        return 0
    return int(data_hex[start:end], 16)


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


def parse_nadfun_token_created(
    addr: str,
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
        last_price_native = Decimal("4500") / Decimal("1073000191")
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


def parse_nadfun_sync(
    addr: str,
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


def parse_nadfun_buy(
    addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    print("buy", topics, data_no0x)
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


def parse_nadfun_sell(
    addr: str,
    topics: list[str],
    data_no0x: str,
) -> Optional[dict]:
    print("sell", topics, data_no0x)
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


def parse_nadfun_graduated(
    addr: str,
    topics: list[str],
    data_no0x: str,
) -> dict:
    token = _to_addr(topics[1]) if len(topics) > 1 else ""
    return {"token": token}
