from __future__ import annotations
import os
import time
import json
import httpx
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

API_KEY = os.getenv("X_BEARER_TOKEN", "new1_455ca2672bbe4493808f4704350723cc")

CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_MS = 1000 * 60 * 15

def _cache_get(key: str) -> Optional[Any]:
    item = CACHE.get(key)
    if not item:
        return None
    if time.time() * 1000 > item["exp"]:
        CACHE.pop(key, None)
        return None
    return item["data"]


def _cache_set(key: str, data: Any) -> None:
    CACHE[key] = {"data": data, "exp": int(time.time() * 1000) + CACHE_TTL_MS}


def _respond(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(
        content=payload,
        status_code=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "max-age=900, public",
        },
    )


def _normalize_verified_type(a: dict | None) -> Optional[str]:
    if not a:
        return None
    vt = str(a.get("verifiedType") or a.get("verified_type") or "").lower()
    if vt and vt != "none":
        return vt
    if a.get("isBlueVerified"):
        return "blue"
    if (a.get("affiliatesHighlightedLabel") or {}).get("label", {}).get("user_label_type") == "BusinessLabel":
        return "business"
    if a.get("isVerified"):
        return "blue"
    return None


def _compute_verified_flag(a: dict | None) -> bool:
    if not a:
        return False
    return bool(
        a.get("isBlueVerified")
        or a.get("isVerified")
        or str(a.get("verifiedType") or "").lower() == "business"
        or (a.get("affiliatesHighlightedLabel") or {}).get("label", {}).get("user_label_type") == "BusinessLabel"
    )


def _parse_input(input_s: str) -> Optional[Dict[str, str]]:
    s = (input_s or "").strip()
    if not s:
        return None

    if "://" not in s:
        h = s[1:] if s.startswith("@") else s
        if 1 <= len(h) <= 15 and all(c.isalnum() or c == "_" for c in h):
            return {"kind": "user", "username": h}

    try:
        from urllib.parse import urlparse
        import re
        u = urlparse(s)
        host = (u.hostname or "").lower()
        if not (host.endswith("x.com") or host.endswith("twitter.com")):
            return None

        m = re.match(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)", u.path)
        if m:
            return {"kind": "tweet", "id": m.group(2)}

        mc = re.match(r"^/i/communities/(\d+)", u.path)
        if mc:
            return {"kind": "community", "id": mc.group(1)}

        mu = re.match(r"^/([A-Za-z0-9_]{1,15})(?:/|$)", u.path)
        if mu:
            return {"kind": "user", "username": mu.group(1)}

        return None
    except Exception:
        return None


async def _fetch_with_retry(url: str, headers: Dict[str, str], retries: int = 1) -> Tuple[int, str]:
    if not httpx:
        raise RuntimeError("httpx not installed; add `httpx` to requirements.txt")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 429 and retries > 0:
            await asyncio_sleep(5.0)
            return await _fetch_with_retry(url, headers, retries - 1)
        return r.status_code, r.text


async def asyncio_sleep(sec: float) -> None:
    import asyncio
    await asyncio.sleep(sec)


@router.post("/x")
async def x_post(req: Request):
    q = req.query_params.get("clear") or ""
    if q == "1":
        CACHE.clear()
        return _respond({"message": "Backend CACHE cleared ✅"})
    return _respond({"error": "Missing ?clear=1 param"}, 400)


@router.get("/x")
async def x_get(req: Request):
    if not API_KEY:
        return _respond({"error": "Missing X_BEARER_TOKEN"}, 500)

    url_q = (req.query_params.get("url") or "").strip()
    if not url_q:
        return _respond({"error": "Missing url"}, 400)

    parsed = _parse_input(url_q)
    if not parsed:
        return _respond({"error": "Unsupported X/Twitter URL or handle"}, 422)

    cache_key = json.dumps(parsed, sort_keys=True)
    cached = _cache_get(cache_key)
    if cached:
        return _respond(cached)

    headers = {"X-API-Key": API_KEY}
    try:
        if parsed["kind"] == "user":
            url = f"https://api.twitterapi.io/twitter/user/info?userName={parsed['username']}"
            status, text = await _fetch_with_retry(url, headers)
            if status < 200 or status >= 300:
                return _respond({"error": f"HTTP {status}: {text}"}, status)
            j = json.loads(text)
            u = j.get("data")
            if not u:
                return _respond({"error": "User not found"}, 404)

            payload = {
                "kind": "user",
                "user": {
                    "id": u.get("id"),
                    "name": u.get("name"),
                    "username": u.get("userName"),
                    "avatar": u.get("profilePicture"),
                    "banner": u.get("coverPicture") or None,
                    "verified": _compute_verified_flag(u),
                    "verified_type": _normalize_verified_type(u),
                    "created_at": u.get("createdAt"),
                    "followers": u.get("followers"),
                    "following": u.get("following"),
                    "description": u.get("description") or "",
                    "location": u.get("location") or "",
                    "url": url_q,
                },
            }
            _cache_set(cache_key, payload)
            return _respond(payload)

        if parsed["kind"] == "tweet":
            url = f"https://api.twitterapi.io/twitter/tweets?tweet_ids={parsed['id']}"
            status, text = await _fetch_with_retry(url, headers)
            if status < 200 or status >= 300:
                return _respond({"error": f"HTTP {status}: {text}"}, status)
            j = json.loads(text)
            t = (j.get("tweets") or [None])[0]
            if not t:
                return _respond({"error": "Tweet not found"}, 404)

            media_items = (t.get("extendedEntities") or {}).get("media") or []
            media: list[dict] = []
            for m in media_items:
                mtype = m.get("type")
                if mtype == "photo":
                    media.append({
                        "type": "photo",
                        "url": m.get("media_url_https") or m.get("media_url"),
                    })
                elif mtype in ("video", "animated_gif"):
                    variants = ((m.get("video_info") or {}).get("variants") or [])
                    mp4s = [v for v in variants if (v.get("content_type") == "video/mp4")]
                    mp4s.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
                    url_pick = (mp4s[0]["url"] if mp4s else (m.get("url")))
                    media.append({"type": mtype, "url": url_pick})

            author = t.get("author")
            author_payload = None
            if author:
                author_payload = {
                    "id": author.get("id"),
                    "name": author.get("name"),
                    "username": author.get("userName"),
                    "avatar": author.get("profilePicture"),
                    "verified": _compute_verified_flag(author),
                    "verified_type": _normalize_verified_type(author),
                    "followers": author.get("followers"),
                    "following": author.get("following"),
                    "created_at": author.get("createdAt"),
                }

            payload = {
                "kind": "tweet",
                "tweet": {
                    "id": t.get("id"),
                    "text": t.get("text"),
                    "created_at": t.get("createdAt"),
                    "metrics": {
                        "reply_count": t.get("replyCount") or t.get("reply_count") or 0,
                        "retweet_count": t.get("retweetCount") or t.get("retweet_count") or 0,
                        "like_count": t.get("likeCount") or t.get("like_count") or 0,
                    },
                    "possibly_sensitive": bool(t.get("possiblySensitive")),
                    "is_reply": bool(t.get("isReply")),
                    "in_reply_to_username": t.get("inReplyToUsername") or "",
                    "media": media,
                },
                "author": author_payload,
                "url": url_q,
            }
            _cache_set(cache_key, payload)
            return _respond(payload)

        if parsed["kind"] == "community":
            url = f"https://api.twitterapi.io/twitter/community/info?community_id={parsed['id']}"
            status, text = await _fetch_with_retry(url, headers)
            if status < 200 or status >= 300:
                return _respond({"error": f"HTTP {status}: {text}"}, status)
            j = json.loads(text)
            comm = j.get("community_info")
            if not comm:
                return _respond({"error": "Community not found"}, 404)

            def parse_user(obj: Optional[dict]) -> Optional[dict]:
                if not obj:
                    return None
                u = obj.get("user") or obj
                return {
                    "id": u.get("id"),
                    "name": u.get("name"),
                    "username": u.get("userName") or u.get("username") or u.get("screen_name") or "",
                    "avatar": u.get("profilePicture") or u.get("profile_image_url") or u.get("profile_image_url_https"),
                    "verified": _compute_verified_flag(u),
                    "verified_type": _normalize_verified_type(u),
                    "description": u.get("description") or "",
                    "followers": u.get("followers") or u.get("followers_count") or 0,
                    "following": u.get("following") or u.get("following_count") or u.get("friends_count") or 0,
                    "created_at": u.get("createdAt") or u.get("created_at"),
                }

            payload = {
                "kind": "community",
                "community": {
                    "id": comm.get("id"),
                    "name": comm.get("name"),
                    "description": comm.get("description") or "",
                    "member_count": comm.get("member_count") or 0,
                    "created_at": comm.get("created_at"),
                    "banner_url": comm.get("banner_url") or None,
                    "creator": parse_user(comm.get("creator")),
                    "admin": parse_user(comm.get("admin")),
                    "members_preview": comm.get("members_preview") or [],
                },
                "url": url_q,
            }
            _cache_set(cache_key, payload)
            return _respond(payload)

        return _respond({"error": "Unhandled kind"}, 500)

    except Exception as e:
        return _respond({"error": getattr(e, "message", repr(e))}, 500)
