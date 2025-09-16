import asyncio
import json
from collections import defaultdict
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

STATS_BASE_URL = "http://127.0.0.1:8000"
POLL_SECONDS = 5.0

_subscribers: Dict[str, Set[WebSocket]] = defaultdict(set)
_tasks: Dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()


def _fetch_json(url: str) -> dict:
    import urllib.request
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = resp.read().decode("utf-8")
        return json.loads(data)


async def _broadcast(token: str, payload: dict) -> None:
    dead = []
    async with _lock:
        for ws in list(_subscribers[token]):
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        for ws in dead:
            _subscribers[token].discard(ws)


async def _poller(token: str) -> None:
    url = f"{STATS_BASE_URL.rstrip('/')}/stats/{token}"
    try:
        try:
            snap = await asyncio.to_thread(_fetch_json, url)
            await _broadcast(token, {"type": "stats", "initial": True, **snap})
        except Exception:
            pass

        while True:
            async with _lock:
                if not _subscribers[token]:
                    break
            try:
                data = await asyncio.to_thread(_fetch_json, url)
                await _broadcast(token, {"type": "stats", **data})
            except Exception:
                pass
            await asyncio.sleep(POLL_SECONDS)
    finally:
        async with _lock:
            _tasks.pop(token, None)


@router.websocket("/ws/stats/{token}")
async def ws_stats(websocket: WebSocket, token: str):
    token = token.lower()
    await websocket.accept()
    async with _lock:
        _subscribers[token].add(websocket)
        if token not in _tasks:
            _tasks[token] = asyncio.create_task(_poller(token))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with _lock:
            _subscribers[token].discard(websocket)