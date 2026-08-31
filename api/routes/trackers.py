from __future__ import annotations

import base64
import binascii
import re

from fastapi import APIRouter, HTTPException, Request

import core.storage as storage

router = APIRouter(prefix="/trackers", tags=["trackers"])

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CIPHERTEXT_BYTES = 262144


def _require_key(key: str) -> str:
    normalized = (key or "").lower()
    if not _KEY_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="invalid tracker key")
    return normalized


def _decode_base64(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail=f"invalid {field}") from None


def _require_payload(body: object) -> tuple[int, str, str, int]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    version = body.get("version")
    iv = body.get("iv")
    ciphertext = body.get("ciphertext")
    updated_at = body.get("updatedAt")
    if version != 1:
        raise HTTPException(status_code=400, detail="unsupported payload version")
    if not isinstance(updated_at, int) or isinstance(updated_at, bool) or updated_at <= 0:
        raise HTTPException(status_code=400, detail="invalid updatedAt")
    iv_bytes = _decode_base64(iv, "iv")
    ciphertext_bytes = _decode_base64(ciphertext, "ciphertext")
    if len(iv_bytes) != 12:
        raise HTTPException(status_code=400, detail="invalid iv")
    if len(ciphertext_bytes) < 16 or len(ciphertext_bytes) > _MAX_CIPHERTEXT_BYTES:
        raise HTTPException(status_code=400, detail="invalid ciphertext")
    return version, iv, ciphertext, updated_at


@router.get("/wallets/{key}")
def wallet_tracker_list(key: str) -> dict:
    normalized = _require_key(key)
    return {"payload": storage.get_wallet_tracker_payload(normalized)}


@router.put("/wallets/{key}")
async def wallet_tracker_save(key: str, request: Request) -> dict:
    normalized = _require_key(key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid payload") from None
    version, iv, ciphertext, updated_at = _require_payload(body)
    stored = storage.put_wallet_tracker_payload(
        normalized,
        version,
        iv,
        ciphertext,
        updated_at,
    )
    return {"ok": True, "stored": stored}
