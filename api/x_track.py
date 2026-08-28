from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from email.utils import parsedate_to_datetime

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

import core.storage as storage
from api.x_api import API_KEY, _compute_verified_flag, _normalize_verified_type

router = APIRouter()

POLL_INTERVAL = float(os.getenv("TRACK_POLL_INTERVAL", "30"))
FANOUT_INTERVAL = float(os.getenv("TRACK_FANOUT_INTERVAL", "2"))
BACKLOG_LIMIT = int(os.getenv("TRACK_BACKLOG_LIMIT", "50"))
LIVE_WINDOW_SECS = int(os.getenv("TRACK_LIVE_WINDOW", "900"))
LEADER_TTL_SECS = int(os.getenv("TRACK_LEADER_TTL", "90"))
SEED_USERS = [u.strip().lstrip("@").lower() for u in os.getenv("TRACKED_USERS", "").split(",") if u.strip()]
LAST_TWEETS_URL = "https://api.twitterapi.io/twitter/user/last_tweets"

NODE_ID = uuid.uuid4().hex
CLIENTS: set[WebSocket] = set()
_TASKS: list[asyncio.Task] = []


def _iso(created_at: str | None) -> str:
    try:
        return parsedate_to_datetime(created_at).isoformat()
    except Exception:
        return created_at or ""


def _epoch(created_at: str | None) -> int:
    try:
        return int(parsedate_to_datetime(created_at).timestamp())
    except Exception:
        return 0


def _tweet_kind(t: dict) -> str:
    if t.get("retweeted_tweet"):
        return "retweet"
    if t.get("isReply"):
        return "reply"
    if t.get("quoted_tweet"):
        return "quote"
    return "tweet"


def _media(t: dict) -> list[dict]:
    out: list[dict] = []
    for m in (t.get("extendedEntities") or {}).get("media") or []:
        kind = m.get("type")
        if kind == "photo":
            out.append({"type": "photo", "url": m.get("media_url_https") or m.get("media_url")})
        elif kind in ("video", "animated_gif"):
            mp4s = sorted(
                [v for v in (m.get("video_info") or {}).get("variants") or [] if v.get("content_type") == "video/mp4"],
                key=lambda v: v.get("bitrate", 0),
                reverse=True,
            )
            out.append({"type": kind, "url": mp4s[0]["url"] if mp4s else m.get("url")})
    return out


def _tweet_payload(username: str, t: dict) -> dict:
    a = t.get("author")
    handle = str((a or {}).get("userName") or username or "").lstrip("@").lower()
    return {
        "type": _tweet_kind(t),
        "username": handle,
        "tweet": {
            "id": str(t.get("id") or ""),
            "text": t.get("text"),
            "created_at": _iso(t.get("createdAt")),
            "url": t.get("url") or t.get("twitterUrl"),
            "metrics": {
                "reply_count": t.get("replyCount") or 0,
                "retweet_count": t.get("retweetCount") or 0,
                "like_count": t.get("likeCount") or 0,
            },
            "media": _media(t),
        },
        "author": {
            "name": a.get("name"),
            "username": a.get("userName"),
            "avatar": a.get("profilePicture"),
            "verified": _compute_verified_flag(a),
            "verified_type": _normalize_verified_type(a),
        }
        if a
        else None,
        "timestamp": _iso(t.get("createdAt")),
    }


async def _broadcast(payload: dict) -> None:
    if not CLIENTS:
        return
    text = json.dumps(payload)
    for ws in list(CLIENTS):
        try:
            await ws.send_text(text)
        except Exception:
            CLIENTS.discard(ws)


async def _poll_user(client: httpx.AsyncClient, username: str) -> int:
    resp = await client.get(LAST_TWEETS_URL, params={"userName": username}, headers={"X-API-Key": API_KEY})
    resp.raise_for_status()
    tweets = ((resp.json() or {}).get("data") or {}).get("tweets") or []
    rows = []
    for t in tweets:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        rows.append((tid, username, _epoch(t.get("createdAt")), _tweet_payload(username, t)))
    storage.insert_x_tweets(rows)
    return len(rows)


async def _poll_loop() -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            try:
                if storage.claim_x_poll_leader(NODE_ID, LEADER_TTL_SECS):
                    for username in storage.list_x_tracked_users():
                        try:
                            await _poll_user(client, username)
                        except Exception as e:
                            print(f"[XTRACK] poll {username} failed: {e!r}", flush=True)
            except Exception as e:
                print(f"[XTRACK] poll cycle failed: {e!r}", flush=True)
            await asyncio.sleep(POLL_INTERVAL)


async def _fanout_loop() -> None:
    cursor = 0
    while True:
        try:
            if not CLIENTS:
                cursor = storage.max_x_tweet_row_id()
            else:
                if cursor <= 0:
                    cursor = storage.max_x_tweet_row_id()
                now = int(time.time())
                for row_id, created_at, payload in storage.list_x_tweets_after(cursor, 200):
                    cursor = row_id
                    if created_at and (now - created_at) > LIVE_WINDOW_SECS:
                        continue
                    await _broadcast(payload)
        except Exception as e:
            print(f"[XTRACK] fanout failed: {e!r}", flush=True)
        await asyncio.sleep(FANOUT_INTERVAL)


def start_workers() -> None:
    if _TASKS:
        return
    try:
        if SEED_USERS:
            storage.add_x_tracked_users(SEED_USERS)
    except Exception as e:
        print(f"[XTRACK] seeding tracked users failed: {e!r}", flush=True)
    _TASKS.append(asyncio.create_task(_poll_loop()))
    _TASKS.append(asyncio.create_task(_fanout_loop()))
    print(f"[XTRACK] workers started, node {NODE_ID[:8]}", flush=True)


def stop_workers() -> None:
    for task in _TASKS:
        task.cancel()
    _TASKS.clear()


async def _requested_usernames(req: Request) -> list[str]:
    try:
        body = await req.json()
    except Exception:
        return []
    if isinstance(body, list):
        raw = body
    else:
        raw = (body or {}).get("usernames") or []
    return [str(u).strip().lstrip("@") for u in raw if str(u).strip()]


@router.get("/x/track")
async def x_track_list():
    return {"tracked": storage.list_x_tracked_users()}


@router.post("/x/track")
async def x_track_add(req: Request):
    storage.add_x_tracked_users(await _requested_usernames(req))
    return {"tracked": storage.list_x_tracked_users()}


@router.delete("/x/track")
async def x_track_remove(req: Request):
    storage.remove_x_tracked_users(await _requested_usernames(req))
    return {"tracked": storage.list_x_tracked_users()}


@router.get("/x/tweets")
async def x_tweets_get(usernames: str = "", limit: int = BACKLOG_LIMIT):
    names = [u.strip() for u in usernames.split(",") if u.strip()]
    if names:
        storage.add_x_tracked_users(names)
    return storage.list_x_recent_tweets(names, max(1, min(int(limit), 200)))


@router.post("/x/tweets")
async def x_tweets_post(req: Request, limit: int = BACKLOG_LIMIT):
    names = await _requested_usernames(req)
    if names:
        storage.add_x_tracked_users(names)
    return storage.list_x_recent_tweets(names, max(1, min(int(limit), 200)))


@router.websocket("/x/ws")
async def x_ws(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    try:
        for payload in reversed(storage.list_x_recent_tweets(None, BACKLOG_LIMIT)):
            await ws.send_text(json.dumps(payload))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        CLIENTS.discard(ws)
