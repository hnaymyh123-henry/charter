"""FastAPI host for Public Charter JSON.

Two deployment modes, sharing one process:

1.  **SaaS / multi-tenant mode** (default). All Charters for all principals
    are served under `{CHARTER_URL_BASE}/{principal_id}/{agent_id}`. Set
    `CHARTER_URL_BASE` to your public origin (e.g. ``https://charter.dev``).

2.  **Self-hosted single-principal mode.** A principal publishes their own
    Charters on their own domain at the AP2/Web-Bot-Auth-style location
    ``/.well-known/charter/{agent_id}``. To opt in, set
    ``CHARTER_SELF_HOSTED_PRINCIPAL=<principal_id>``; the route then resolves
    Charters under that principal only.

A directory lookup endpoint at ``/api/lookup`` resolves
``(principal_id, agent_id) -> charter_url`` for SDK consumers that don't
already know the URL.

A health endpoint at ``/healthz`` returns ``{"ok": true}`` for liveness/
readiness probes.
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .constants import DEFAULT_URL_BASE
from .storage import list_charters, load_charter

app = FastAPI(title="Charter Service", version="0.1.0")


def _self_hosted_principal() -> str | None:
    """Return the single principal this server is self-hosting, or None
    if the server is running in the default multi-tenant mode."""
    value = os.environ.get("CHARTER_SELF_HOSTED_PRINCIPAL", "").strip()
    return value or None


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness/readiness probe. Always returns 200 if the process is up."""
    return {"ok": True}


@app.get("/")
def root() -> dict[str, Any]:
    """Index — list all hosted Charters."""
    chs = list_charters()
    return {
        "service": "Charter Service",
        "self_hosted_principal": _self_hosted_principal(),
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
def lookup(principal_id: str, agent_id: str) -> dict[str, str]:
    """Resolve (principal_id, agent_id) -> charter_url."""
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        raise HTTPException(status_code=404, detail="charter not found")
    base = os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE).rstrip("/")
    return {
        "charter_url": f"{base}/{principal_id}/{agent_id}",
        "charter_id": charter.charter_id,
    }


@app.get("/.well-known/charter/{agent_id}")
def well_known_charter(agent_id: str) -> JSONResponse:
    """Self-hosted Charter discovery.

    Returns this principal's Charter for the requested `agent_id`. Only
    available when `CHARTER_SELF_HOSTED_PRINCIPAL` is set — otherwise
    returns 404 (the server has no single principal to resolve to).

    Mirrors the Web Bot Auth `/.well-known/` discovery pattern: a principal
    serves their own Charters on their own domain without needing a
    multi-tenant directory.
    """
    principal_id = _self_hosted_principal()
    if principal_id is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Self-hosted mode is not enabled on this server. "
                "Set CHARTER_SELF_HOSTED_PRINCIPAL to opt in."
            ),
        )
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        raise HTTPException(status_code=404, detail="charter not found")
    return JSONResponse(content=charter.model_dump(mode="json"))


@app.get("/{principal_id}/{agent_id}")
def get_charter(principal_id: str, agent_id: str) -> JSONResponse:
    """Public Charter JSON endpoint. Pretty-printed for human inspection."""
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        raise HTTPException(status_code=404, detail="charter not found")
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
