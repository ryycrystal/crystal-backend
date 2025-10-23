from __future__ import annotations
from typing import Dict, Any, List

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from core.sequencer import SEQUENCER
from state import INTERVALS, LABEL
from api.ws import router as ws_router
from api.x_api import router as x_router

app = FastAPI(title="pre-migration launchpad token stats", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router)
app.include_router(x_router)

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}

@app.get("/stats/{token}")
def stats_for_token(token: str) -> Dict[str, Dict[str, Any]]:
    snap = SEQUENCER._state.snapshot(token.lower())
    return {LABEL[h]: snap[h] for h in INTERVALS}

@app.get("/stats")
def stats_batch(tokens: List[str] = Query(...)) -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for t in tokens:
        snap = SEQUENCER._state.snapshot(t.lower())
        out[t.lower()] = {LABEL[h]: snap[h] for h in INTERVALS}
    return out

@app.get("/debug/tokens")
def debug_tokens() -> Dict[str, int]:
    return SEQUENCER._state.debug_tokens()

@app.get("/vaults")
def list_vaults() -> Dict[str, List[str]]:
    meta = SEQUENCER._state.vault_meta()
    return {v: [q, b] for v, (q, b) in meta.items()}

@app.get("/vaults/{vault}/latest")
def vault_latest(vault: str) -> Dict[str, Any]:
    return SEQUENCER._state.vault_latest_minute(vault)

@app.get("/vaults/{vault}/series")
def vault_series(vault: str, horizon: str = Query("7d", regex="^(1d|7d|14d|30d)$")) -> List[Dict[str, Any]]:
    return SEQUENCER._state.vault_series(vault, horizon)
