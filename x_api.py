from __future__ import annotations
import os, time, json
from typing import Any, Dict, Optional, Tuple
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

try:
    import httpx
except Exception:
    httpx = None

router = APIRouter()

BEARER = os.getenv("X_BEARER_TOKEN")

class _Entry:
    __slots__ = ("value", "exp", "stale_exp")

    def __init__(self, value: Any, exp: int, stale_exp: int) -> None:
        self.value = value
        self.exp = exp
        self.stale_exp = stale_exp


CACHE: Dict[str, _Entry] = {}

PROF_TTL = 6 * 3600
PROF_STALE = 24 * 3600
TWEET_TTL = 3600
TWEET_STALE = 6 * 3600


def _now() -> int:
    return int(time.time())


def _cache_get(key: str) -> Optional[Any]:
    e = CACHE.get(key)
    if not e:
        return None
    n = _now()
    if e.exp > n:
        return e.value
    if e.stale_exp > n:
        v = dict(e.value)
        v["_stale"] = True
        return v
    CACHE.pop(key, None)
    return None


def _cache_set(key: str, value: Any, fresh_ttl: int, stale_ttl: int) -> None:
    n = _now()
    CACHE[key] = _Entry(value, n + fresh_ttl, n + stale_ttl)


def _parse_input(input_s: str) -> Optional[Dict[str, str]]:
    s = input_s.strip()
    if not s:
        return None

    if "://" not in s:
        h = s[1:] if s.startswith("@") else s
        if 1 <= len(h) <= 15 and all(c.isalnum() or c == "_" for c in h):
            return {"kind": "user", "username": h}

    try:
        from urllib.parse import urlparse

        u = urlparse(s)
        host = (u.hostname or "").lower()
        if not (host.endswith("x.com") or host.endswith("twitter.com")):
            return None

        import re

        t = re.match(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)", u.path)
        if t:
            return {"kind": "tweet", "id": t.group(2)}

        m = re.match(r"^/([A-Za-z0-9_]{1,15})(?:/|$)", u.path)
        if m:
            return {"kind": "user", "username": m.group(1)}
        return None
    except Exception:
        return None


def _json_response(
    payload: Any, status: int = 200, s_maxage: int = 3600
) -> JSONResponse:
    return JSONResponse(
        content=payload,
        status_code=status,
        headers={
            "Cache-Control": f"public, s-maxage={s_maxage}, stale-while-revalidate=86400",
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _process_media_url(media: dict) -> Optional[str]:
    mtype = media.get("type")
    if mtype == "photo":
        return media.get("url") or media.get("preview_image_url")

    if mtype in ("video", "animated_gif"):
        variants = media.get("variants") or []
        vids = [
            v
            for v in variants
            if v.get("url") and "video" in (v.get("content_type") or "")
        ]
        vids.sort(key=lambda v: v.get("bit_rate", 0), reverse=True)
        if vids:
            for v in vids:
                if v.get("content_type") == "video/mp4" or (".mp4" in v.get("url", "")):
                    return v["url"]
            return vids[0]["url"]

        return media.get("url") or media.get("preview_image_url")

    return media.get("url") or media.get("preview_image_url")


async def _twitter_call(url: str, bearer: str) -> Tuple[int, str]:
    if not httpx:
        raise HTTPException(
            status_code=500,
            detail="httpx not installed; add `httpx` to requirements.txt",
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {bearer}"})
        return r.status_code, r.text

@router.get("/x")
async def x_resolve(req: Request):
    q = req.query_params.get("url", "") or ""
    if not q:
        return _json_response({"error": "Missing url"}, 400)
    if not BEARER:
        return _json_response({"error": "Missing X_BEARER_TOKEN on server"}, 500)

    parsed = _parse_input(q)
    if not parsed:
        return _json_response({"error": "Unsupported X/Twitter URL or handle"}, 422)

    key = (
        f"u:{parsed['username'].lower()}"
        if parsed["kind"] == "user"
        else f"t:{parsed['id']}"
    )
    cached = _cache_get(key)
    if cached:
        smax = PROF_TTL if parsed["kind"] == "user" else TWEET_TTL
        return _json_response(cached, 200, smaxage=smax)

    try:
        if parsed["kind"] == "user":
            url = (
                f"https://api.twitter.com/2/users/by/username/{parsed['username']}"
                f"?user.fields=profile_image_url,profile_banner_url,verified,verified_type,created_at,"
                f"public_metrics,description,location,url,protected,entities"
            )
            status, text = await _twitter_call(url, BEARER)

            if status == 429:
                stale = _cache_get(key)
                if stale:
                    return _json_response(stale, 200, s_maxage=300)
                return _json_response({"error": "Rate limited (429)"}, 429)

            if status < 200 or status >= 300:
                return _json_response({"error": text or "Upstream error"}, status)

            u = json.loads(text).get("data", {}) or {}
            payload = {
                "kind": "user",
                "user": {
                    "id": u.get("id"),
                    "name": u.get("name"),
                    "username": u.get("username"),
                    "avatar": u.get("profile_image_url"),
                    "banner": u.get("profile_banner_url") or None,
                    "verified": bool(u.get("verified")),
                    "created_at": u.get("created_at"),
                    "followers": (u.get("public_metrics") or {}).get("followers_count"),
                    "following": (u.get("public_metrics") or {}).get("following_count"),
                    "description": u.get("description") or "",
                    "location": u.get("location") or "",
                    "url": q,
                },
            }
            _cache_set(key, payload, PROF_TTL, PROF_STALE)
            return _json_response(payload, 200, s_maxage=PROF_TTL)

        url = (
            f"https://api.twitter.com/2/tweets/{parsed['id']}"
            f"?tweet.fields=created_at,public_metrics,entities,possibly_sensitive"
            f"&expansions=author_id,attachments.media_keys"
            f"&user.fields=name,username,profile_image_url,verified"
            f"&media.fields=preview_image_url,url,width,height,type,variants,duration_ms"
        )
        status, text = await _twitter_call(url, BEARER)

        if status == 429:
            stale = _cache_get(key)
            if stale:
                return _json_response(stale, 200, s_maxage=300)
            return _json_response({"error": "Rate limited (429)"}, 429)

        if status < 200 or status >= 300:
            return _json_response({"error": text or "Upstream error"}, status)

        j = json.loads(text)
        t = j.get("data") or {}
        author = (j.get("includes") or {}).get("users", [None])[0]
        media_in = (j.get("includes") or {}).get("media") or []
        media = [
            {
                "type": m.get("type"),
                "url": _process_media_url(m),
                "width": m.get("width"),
                "height": m.get("height"),
                "duration_ms": m.get("duration_ms"),
            }
            for m in media_in
        ]

        payload = {
            "kind": "tweet",
            "tweet": {
                "id": t.get("id"),
                "text": t.get("text"),
                "created_at": t.get("created_at"),
                "metrics": t.get("public_metrics") or {},
                "possibly_sensitive": bool(t.get("possibly_sensitive")),
                "media": media,
            },
            "author": (
                {
                    "id": author.get("id"),
                    "name": author.get("name"),
                    "username": author.get("username"),
                    "avatar": author.get("profile_image_url"),
                    "verified": bool(author.get("verified")),
                }
                if author
                else None
            ),
            "url": q,
        }

        _cache_set(key, payload, TWEET_TTL, TWEET_STALE)
        return _json_response(payload, 200, s_maxage=TWEET_TTL)

    except HTTPException:
        raise
    except Exception as e:
        return _json_response({"error": getattr(e, "message", repr(e))}, 500)
