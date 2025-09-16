from __future__ import annotations
from typing import Dict, Any, List

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from sequencer import SEQUENCER
from state import INTERVALS, LABEL
from ws import router as ws_router

app = FastAPI(title="Pre-Migration Launchpad TOken Stats", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router)

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}

@app.get("/stats/{token}")
def stats_for_token(token: str) -> Dict[str, Dict[str, int]]:
    snap = SEQUENCER._state.snapshot(token.lower())
    return { LABEL[h]: snap[h] for h in INTERVALS }

@app.get("/stats")
def stats_batch(tokens: List[str] = Query(...)) -> Dict[str, Dict[str, Dict[str, int]]]:
    out: Dict[str, Dict[str, Dict[str, int]]] = {}
    for t in tokens:
        snap = SEQUENCER._state.snapshot(t.lower())
        out[t.lower()] = { LABEL[h]: snap[h] for h in INTERVALS }
    return out

@app.get("/debug/tokens")
def debug_tokens() -> Dict[str, int]:
    return SEQUENCER._state.debug_tokens()
