import asyncio
from decimal import Decimal
from typing import List, Dict

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from sequencer import SEQUENCER  # global singleton

app = FastAPI(title="crystal exchange api", version="0.1.0")

# allow the frontend anywhere to hit us
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────── in-memory leaderboard cache (refreshed every 3 s) ────────
LEADERBOARD_CACHE: List[tuple[str, Decimal]] = []

@app.on_event("startup")
async def _spawn_leaderboard_refresher() -> None:
    """background loop that recomputes the leaderboard every 3 s."""
    async def _loop() -> None:
        global LEADERBOARD_CACHE
        while True:
            LEADERBOARD_CACHE = SEQUENCER._state.leaderboard()
            await asyncio.sleep(3)
    asyncio.create_task(_loop())

# ───────────────────────── routes ────────────────────────────────
@app.get("/leaderboard")
def get_leaderboard(
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, List[Dict[str, str]]]:
    """
    return top <limit> makers.  decimals serialised as strings to avoid
    float misery in json.
    """
    board = LEADERBOARD_CACHE[:limit] or SEQUENCER._state.leaderboard()
    return {
        "leaderboard": [
            {"maker": addr, "points": str(pts)} for addr, pts in board
        ]
    }
