"""FastAPI backend for Quantum Portfolio Signals.

Serves the latest signal JSON to the Netlify frontend and exposes a protected
refresh endpoint that runs one six-stock QAOA batch using current Yahoo Finance
daily NSE data.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import quantum_batch_engine as engine

BASE_DIR = Path(__file__).resolve().parent
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

app = FastAPI(title="Quantum Engine API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_lock = threading.Lock()


def _load_json(path: Path, default):
    import json
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def _run_one_batch() -> dict:
    universe = engine.load_universe()
    state = engine.load_state(len(universe))
    master = engine.load_master()
    engine.process_one_batch(universe, state, master)
    engine.save_state(state)
    engine.save_master(master)
    return state


@app.get("/health")
def health():
    return {"status": "ok", "service": "quantumengine-api"}


@app.get("/api/signals")
def signals():
    # Always read from disk so the response reflects the most recent completed batch.
    return _load_json(BASE_DIR / engine.MASTER_FILE, {})


@app.get("/api/state")
def state():
    data = _load_json(BASE_DIR / engine.STATE_FILE, {})
    latest = _load_json(BASE_DIR / engine.LATEST_BATCH_FILE, {})
    return {
        **data,
        "last_run": latest.get("generated_at"),
        "data_source": latest.get("data_source", "Yahoo Finance (.NS tickers)"),
        "market_data_interval": latest.get("market_data_interval", "1d"),
        "engine": latest.get("engine", "QAOA on Qiskit"),
    }


@app.post("/api/refresh")
def refresh(x_refresh_token: str | None = Header(default=None)):
    if not REFRESH_TOKEN:
        raise HTTPException(status_code=503, detail="REFRESH_TOKEN is not configured")
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if not _lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A refresh is already running")
    try:
        updated = _run_one_batch()
        return {
            "ok": True,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "cursor": updated["cursor"],
            "cycle": updated["cycle"],
            "analyzed_this_cycle": updated["analyzed_this_cycle"],
        }
    finally:
        _lock.release()
