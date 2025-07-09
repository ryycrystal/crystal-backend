import asyncio
from decimal import Decimal
from typing import List, Dict

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from sequencer import SEQUENCER

app = FastAPI(title="crystal exchange api", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LEADERBOARD_CACHE: List[tuple[str, Decimal]] = []

@app.on_event("startup")
async def _spawn_leaderboard_refresher() -> None:
    async def _loop() -> None:
        global LEADERBOARD_CACHE
        while True:
            LEADERBOARD_CACHE = SEQUENCER._state.leaderboard()
            await asyncio.sleep(3)
    asyncio.create_task(_loop())

@app.get("/leaderboard")
def get_leaderboard(
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, List[Dict[str, str]]]:
    board = LEADERBOARD_CACHE[:limit] or SEQUENCER._state.leaderboard()
    return {
        "leaderboard": [
            {"maker": addr, "points": str(pts)} for addr, pts in board
        ]
    }
