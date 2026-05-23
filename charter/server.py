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

import json
import os
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import revocation, transparency
from .constants import DEFAULT_URL_BASE
from .signing import public_key_to_jwk
from .storage import list_charters, list_known_issuer_keys, load_charter, load_disclosure

app = FastAPI(title="Charter Service", version="0.1.0")


# Charter response routes that should carry a Cache-Control header so
# callers know how long their cached Charter is permitted to live. The
# middleware below applies the header to responses on these paths.
# Health, JWKS, transparency endpoints are intentionally excluded — they
# have their own freshness contracts.
_CHARTER_RESPONSE_PATHS = {
    "/api/lookup",
}
# Path patterns for the two parameterized Charter routes. Compiled once
# at import time. Matching is intentionally exact (no trailing-slash
# tolerance) to mirror FastAPI's router behavior.
_WELL_KNOWN_CHARTER_RE = re.compile(r"^/\.well-known/charter/[^/]+$")
# `/{principal_id}/{agent_id}` — exactly two non-empty path segments,
# neither beginning with a reserved prefix. The reserved-prefix check is
# defensive: FastAPI already routes `/transparency/...`, `/disclosures/...`,
# `/.well-known/...`, `/api/...`, `/healthz` to their specific handlers
# before the catch-all, but the middleware runs ahead of the router and
# cannot rely on that ordering.
_GET_CHARTER_RE = re.compile(
    r"^/(?!(?:transparency|disclosures|api|healthz)(?:/|$))"
    r"(?!\.well-known(?:/|$))"
    r"[^/]+/[^/]+$"
)


def _cache_ttl_seconds() -> int:
    """Resolve the Cache-Control max-age for Charter responses.

    Default 300s (5 min). Operators can tune via ``CHARTER_CACHE_TTL``;
    values <= 0 disable the header entirely (return 0 here -> middleware
    skips). Non-numeric / negative env values silently fall back to the
    default rather than crashing the request — log-and-continue is the
    operator-friendly choice for a non-critical header.
    """
    raw = os.environ.get("CHARTER_CACHE_TTL", "").strip()
    if not raw:
        return 300
    try:
        ttl = int(raw)
    except ValueError:
        return 300
    return max(0, ttl)


def _is_charter_response_path(path: str) -> bool:
    """True iff `path` is one of the three Charter-returning routes.

    Used by the Cache-Control middleware below. Kept as a pure function
    so it's trivially unit-testable without spinning up the app.
    """
    if path in _CHARTER_RESPONSE_PATHS:
        return True
    if _WELL_KNOWN_CHARTER_RE.match(path):
        return True
    return bool(_GET_CHARTER_RE.match(path))


@app.middleware("http")
async def _cache_control_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Stamp `Cache-Control` on responses for Charter-returning routes.

    Applies only to 200 responses on the three read paths so that 404s
    and 5xx are not cached. The header is added at the middleware layer
    so the existing route bodies stay untouched — that keeps this PR
    bisectable from the parallel route work in #39 / #14.
    """
    response = await call_next(request)
    if response.status_code != 200:
        return response
    if not _is_charter_response_path(request.url.path):
        return response
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return response
    response.headers["Cache-Control"] = f"max-age={ttl}, must-revalidate"
    return response


def _self_hosted_principal() -> str | None:
    """Return the single principal this server is self-hosting, or None
    if the server is running in the default multi-tenant mode."""
    value = os.environ.get("CHARTER_SELF_HOSTED_PRINCIPAL", "").strip()
    return value or None


def _disclosure_token() -> str | None:
    """Return the configured Disclosure bearer token, or None if unset.

    Empty / whitespace-only env -> None -> every `/disclosures/...`
    request returns 404. Operating without a token is the safe default:
    without `CHARTER_DISCLOSURE_TOKEN` configured the issuer simply
    does not expose ADR-011 plaintexts over HTTP, even if Disclosure
    files sit on disk.
    """
    raw = os.environ.get("CHARTER_DISCLOSURE_TOKEN", "").strip()
    return raw or None


def _authorize_disclosure_or_404(authorization: str | None) -> None:
    """Bearer-token check for `/disclosures/...`.

    Mismatches and missing tokens BOTH translate into a 404 (not 401)
    so an attacker without the token cannot tell whether the endpoint
    exists or which disclosure ids are valid. The cost of this choice
    is that legitimate misconfigurations (e.g. operator forgot to set
    the env var) also look like 404; this is documented in
    PRODUCT.md §4.6.
    """
    expected = _disclosure_token()
    if expected is None:
        raise HTTPException(status_code=404, detail="disclosure not found")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=404, detail="disclosure not found")
    presented = authorization[len("Bearer ") :].strip()
    # Constant-time compare to avoid timing oracles on the token.
    import hmac

    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=404, detail="disclosure not found")


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


@app.get("/.well-known/jwks.json")
def well_known_jwks() -> dict[str, list[dict[str, str]]]:
    """JSON Web Key Set per RFC 7517 — the issuer's public keys.

    Charters carry `provenance.issuer_kid`; callers fetch this JWKS and
    look up the matching key by `kid` to verify the signature without
    trusting the inline `issuer_public_key`. That closes the v0 TOFU
    gap: a rotated key won't match a pinned `kid`, and a wrong key in
    the JWKS won't match the Charter's `kid`.

    In **self-hosted mode** (`CHARTER_SELF_HOSTED_PRINCIPAL` set), the
    JWKS is filtered to that principal's keys only. In **multi-tenant
    mode**, the server exposes every issuer key it knows about; each
    JWK carries an `iss` extension field naming the principal so
    callers can match by `(iss, kid)`.
    """
    principal_filter = _self_hosted_principal()
    keys: list[dict[str, str]] = []
    for principal_id, public_key_str in list_known_issuer_keys():
        if principal_filter is not None and principal_id != principal_filter:
            continue
        jwk = public_key_to_jwk(public_key_str)
        # Non-standard extension. `iss` is widely understood as "issuer"
        # in JWT/JWS contexts and is the natural field name here.
        jwk["iss"] = principal_id
        keys.append(jwk)
    return {"keys": keys}


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


@app.get("/transparency/head")
def transparency_head() -> dict[str, Any]:
    """Return the most recent transparency-log entry's metadata.

    Lets a client cheaply check "is the log at the seq I last verified?"
    before deciding to pull `/transparency/log`. Empty log returns
    `{seq: 0, entry_hash: <genesis>, appended_at: null}` rather than
    404, so clients can poll without special-casing first-run.
    """
    h = transparency.head()
    if h is None:
        return {
            "seq": 0,
            "entry_hash": transparency.GENESIS_PREV_HASH,
            "appended_at": None,
        }
    return {
        "seq": h.seq,
        "entry_hash": h.entry_hash,
        "appended_at": h.appended_at.isoformat(),
    }


@app.get("/transparency/log")
def transparency_log(
    since: int = Query(default=0, ge=0, description="Skip entries with seq <= since."),
) -> StreamingResponse:
    """Stream the transparency log as `application/x-ndjson`.

    One JSON object per line, ordered by `seq`. `?since=N` skips
    entries with `seq <= N` (use the last seq the client already
    verified to do incremental syncs).

    No auth in v0.8 — the log is intentionally public; that's the
    whole point of transparency.
    """

    def _iter() -> Iterator[str]:
        for entry in transparency.read_log():
            if entry.seq <= since:
                continue
            yield json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"

    return StreamingResponse(_iter(), media_type="application/x-ndjson")


@app.get("/transparency/revoked")
def transparency_revoked(
    since: str = Query(
        default="0",
        description=(
            "Skip revocation entries with seq <= since. Negative or non-integer values return 400."
        ),
    ),
) -> StreamingResponse:
    """Stream the revoked-Charter feed as `application/x-ndjson`.

    One JSON object per line, ordered by transparency-log `seq`:

        {"charter_id": "...", "principal_id": "...",
         "agent_id": "...", "revoked_at": "...", "seq": N}

    Use ``?since=N`` (the last seq you already consumed) for incremental
    polls. The body is derived live from the transparency log + Charter
    files on every request; there is no separate revocation file (see
    ADR-007: revocation info travels through the transparency log).

    Fail-closed input validation: ``since`` MUST parse to a non-negative
    integer. Anything else returns 400 — a malformed cursor would
    otherwise silently return the whole feed, which a careless client
    might then mis-handle as "no recent revocations".
    """
    # FastAPI's Query(ge=0) returns 422; the spec wants 400. Validate
    # by hand so the error contract matches.
    try:
        since_int = int(since)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"`since` must be an integer, got {since!r}"
        ) from e
    if since_int < 0:
        raise HTTPException(
            status_code=400,
            detail=f"`since` must be non-negative, got {since_int}",
        )

    def _iter() -> Iterator[str]:
        for entry in revocation.iter_revoked_entries(since_int):
            yield json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"

    return StreamingResponse(_iter(), media_type="application/x-ndjson")


@app.get("/transparency/proof/{charter_id:path}")
def transparency_proof(charter_id: str) -> dict[str, Any]:
    """Return the inclusion proof for one Charter.

    Body shape:

        {
          "entry": { ...the full transparency entry... },
          "chain": [
            {"seq": 1, "entry_hash": "sha256:..."},
            ...
            {"seq": N, "entry_hash": "sha256:..."}  // = entry.seq
          ]
        }

    `chain` lists every entry's `entry_hash` from `seq=1` up to and
    including the target's `seq`. A client can independently fetch
    `/transparency/log` and recompute each entry's hash from its
    canonical JSON; the chain here is just enough to bind the
    target's `entry_hash` to a sequence position.

    In v0.8 this is a linear-chain proof. Merkle-tree proofs with
    `O(log n)` size are on the v0.9+ backlog.

    Uses `:path` so charter ids that contain colons (the canonical
    format `charter:principal:agent:date`) don't get rejected by
    FastAPI's default segment matcher.
    """
    target = transparency.get_entry(charter_id)
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"no transparency entry for charter_id={charter_id!r}"
        )
    chain: list[dict[str, Any]] = []
    for entry in transparency.read_log():
        chain.append({"seq": entry.seq, "entry_hash": entry.entry_hash})
        if entry.seq >= target.seq:
            break
    return {"entry": target.to_dict(), "chain": chain}


@app.get("/disclosures/{charter_id:path}/{disclosure_id}")
def get_disclosure(
    charter_id: str,
    disclosure_id: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Return one ADR-011 path 1 Disclosure plaintext.

    Auth: ``Authorization: Bearer <CHARTER_DISCLOSURE_TOKEN>``. The
    env var is the entire access-control surface in v0.9 — there is
    no per-disclosure ACL, no rotation, no audit log of who fetched
    what. Operators that need finer-grained control should put a
    reverse proxy in front.

    Failure modes ALL return the same 404 body so an attacker without
    the token cannot distinguish:
      - env var unset / wrong token
      - disclosure id never existed
      - charter id never existed
      - disclosure file on disk is corrupt

    `charter_id` uses `{charter_id:path}` because the canonical
    format `charter:principal:agent:date` contains colons that
    FastAPI's default segment matcher rejects.
    """
    _authorize_disclosure_or_404(authorization)
    disclosure = load_disclosure(charter_id, disclosure_id)
    if disclosure is None:
        raise HTTPException(status_code=404, detail="disclosure not found")
    return JSONResponse(content=disclosure.model_dump(mode="json"))


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
