"""FastAPI host for Public Charter JSON.

In v0 the host runs on localhost:8000. The URL shape is
`http://localhost:8000/{principal_id}/{agent_id}` — calling agents fetch
this via plain HTTPS GET in production, plain HTTP GET in demo.

A directory lookup endpoint at `/api/lookup` resolves
`(principal_id, agent_id) -> charter_url` for the `resolve_charter_url`
SDK helper. v0 implements this as the simplest possible thing: same URL
shape, no extra index file.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .constants import DEFAULT_URL_BASE
from .storage import list_charters, load_charter


app = FastAPI(title="Charter Service", version="0.1.0")


@app.get("/")
def root() -> dict:
    """Index — list all hosted Charters."""
    chs = list_charters()
    return {
        "service": "Charter Service (v0 demo)",
        "count": len(chs),
        "charters": [
            {
                "charter_id": c.charter_id,
                "principal_id": c.binding.principal_id,
                "agent_id": c.binding.agent_id,
                "status": c.lifecycle.status,
                "url": f"/{c.binding.principal_id}/{c.binding.agent_id}",
            }
            for c in chs
        ],
    }


@app.get("/api/lookup")
def lookup(principal_id: str, agent_id: str) -> dict:
    """Resolve (principal_id, agent_id) -> charter_url."""
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        raise HTTPException(status_code=404, detail="charter not found")
    base = os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE).rstrip("/")
    return {
        "charter_url": f"{base}/{principal_id}/{agent_id}",
        "charter_id": charter.charter_id,
    }


@app.get("/{principal_id}/{agent_id}")
def get_charter(principal_id: str, agent_id: str) -> JSONResponse:
    """Public Charter JSON endpoint. Pretty-printed for human inspection."""
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        raise HTTPException(status_code=404, detail="charter not found")
    # Pretty-print so a browser shows a readable Charter at the URL.
    return JSONResponse(content=charter.model_dump(mode="json"))


def run() -> None:
    """Console-script entry point: `charter-server`."""
    uvicorn.run(
        "charter.server:app",
        host="0.0.0.0",
        port=int(os.environ.get("CHARTER_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    run()
