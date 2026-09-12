from __future__ import annotations

import os
import posixpath
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()

_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_PRIVILEGED_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-admin-key", "x-admin-token", "x-api-key"}
_DENIED_PATHS = {"/integrity", "/" + os.getenv("REWARDS_PATH_PREFIX", "results").strip("/") + "/config"}
_DENIED_PREFIXES = ("/debug/", "/spot/", "/trackers/", "/wallet-prefs/", "/vaults/", "/x")


class BatchCall(BaseModel):
    id: str | int | None = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class BatchRequest(BaseModel):
    requests: list[BatchCall]


def _validate_call(call: BatchCall) -> None:
    path = call.path
    parsed = urlsplit(path)
    if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc:
        raise HTTPException(status_code=400, detail="Batch paths must be relative API paths")
    normalized = "/" + posixpath.normpath(unquote(parsed.path)).lstrip("/")
    normalized = normalized.rstrip("/") or "/"
    if normalized == "/batch":
        raise HTTPException(status_code=400, detail="Nested batch calls are not allowed")
    if normalized in _DENIED_PATHS or normalized.startswith(_DENIED_PREFIXES):
        raise HTTPException(status_code=403, detail=f"Route is not available through batch: {normalized}")
    if normalized.startswith("/user/") and any(
        value.lower() in {"1", "true", "yes", "on"} for value in parse_qs(parsed.query).get("include_native", [])
    ):
        raise HTTPException(status_code=403, detail="RPC-backed user reads are not available through batch")
    if any(header.lower() in _PRIVILEGED_HEADERS for header in call.headers):
        raise HTTPException(status_code=403, detail="Privileged headers are not allowed in batch requests")


@contextmanager
def _database_transaction():
    from core.storage.base import db_transaction

    with db_transaction(read_only=True):
        yield


class _BatchFailed(Exception):
    def __init__(self, index: int) -> None:
        self.index = index


@router.post("/batch")
async def batch(payload: BatchRequest, request: Request) -> dict[str, Any]:
    if not payload.requests or len(payload.requests) > 20:
        raise HTTPException(status_code=400, detail="Batch must contain between 1 and 20 requests")
    for call in payload.requests:
        _validate_call(call)
        if call.method not in ("GET", "HEAD"):
            raise HTTPException(status_code=400, detail="Atomic batches only support GET and HEAD requests")

    responses = []
    try:
        with _database_transaction():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=request.app, raise_app_exceptions=False),
                base_url=str(request.base_url),
            ) as client:
                for index, call in enumerate(payload.requests):
                    headers = {
                        key: value for key, value in call.headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS
                    }
                    response = await client.request(call.method, call.path, headers=headers)
                    try:
                        body = response.json()
                    except ValueError:
                        body = response.text
                    responses.append(
                        {
                            "id": call.id,
                            "status": response.status_code,
                            "headers": dict(response.headers),
                            "body": body,
                        }
                    )
                    if response.status_code >= 400:
                        raise _BatchFailed(index)
    except _BatchFailed as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "atomic batch rolled back", "index": exc.index, "responses": responses},
        )
    return {"responses": responses}
