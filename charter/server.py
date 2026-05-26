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
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import revocation, transparency
from .constants import DEFAULT_URL_BASE
from .errors import (
    CharterError,
    CharterGrantExpiredError,
    CharterGrantNotFoundError,
    CharterGrantSignatureError,
    CharterSchemaError,
    CharterSignatureError,
)
from .grants import load_grant as _load_grant
from .grants import save_grant as _save_grant
from .schema import AdHocGrant, AdHocGrantRequest, AdHocGrantResponse
from .signing import public_key_to_jwk, sign_grant
from .storage import (
    ensure_issuer_key,
    list_charters,
    list_known_issuer_keys,
    load_charter,
    load_disclosure,
)

app = FastAPI(title="Charter Service", version="0.1.0")

# Mount the Inspector's static assets (css + js). The directory is shipped
# with the package, so the path is anchored on this module's location and
# the mount works regardless of the operator's CWD. The mount is always
# registered — even if jinja2 isn't installed — so an admin who later
# `pip install charter[inspector]`s a running deployment does NOT need
# to restart anything for the assets to become reachable.
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "inspector"
if _STATIC_DIR.is_dir():
    app.mount(
        "/static/inspector",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="inspector-static",
    )


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


# ---------------------------------------------------------------------------
# Step-up protocol (B2.5 — ADR-013)
#
# `POST /step-up` is the dual of `propose_within_scope`. The caller asks
# the issuer for a short-TTL, task-bound signed grant that temporarily
# widens authority for one specific task. The grant is signed with the
# same Ed25519 issuer key as the linked Charter (ADR-002), has its own
# canonical-bytes rule (ADR-003 extension), and has no `revoked`
# lifecycle state — short TTL is the only safety primitive.
#
# Three approval modes, gated by env `CHARTER_STEPUP_APPROVAL_MODE`:
#   - `auto-deny` (default, production-safe): refuse every request.
#   - `auto-approve` (dev/test): sign and return immediately.
#   - `callback`: synchronously POST the request to
#     `CHARTER_STEPUP_CALLBACK_URL`; the response shape mirrors
#     `AdHocGrantResponse` and decides outcome.
#
# Rate limit: `(principal_id, agent_id)` -> ≤5 requests / 60s (in-memory
# token bucket; single-process by design — multi-instance is future).
# ---------------------------------------------------------------------------


_STEPUP_DEFAULT_MAX_TTL = 3600
_STEPUP_RATE_WINDOW_SEC = 60.0
_STEPUP_RATE_MAX = 5

# In-memory rate-limit state. Key: (principal_id, agent_id) -> deque of
# monotonic timestamps. Guarded by `_stepup_rate_lock` so concurrent
# requests on the same key cannot both pass the bucket check.
_stepup_rate_lock = threading.Lock()
_stepup_rate_state: dict[tuple[str, str], deque[float]] = {}


def _stepup_max_ttl() -> int:
    """Resolve the max TTL cap (`CHARTER_STEPUP_MAX_TTL`, default 3600s).

    Non-numeric / non-positive values silently fall back to the default
    — operator-friendly, since this is a safety cap, not a request param.
    """
    raw = os.environ.get("CHARTER_STEPUP_MAX_TTL", "").strip()
    if not raw:
        return _STEPUP_DEFAULT_MAX_TTL
    try:
        ttl = int(raw)
    except ValueError:
        return _STEPUP_DEFAULT_MAX_TTL
    return ttl if ttl > 0 else _STEPUP_DEFAULT_MAX_TTL


def _stepup_approval_mode() -> str:
    """Resolve approval mode env. Defaults to `auto-deny` (production-safe).

    Unknown values fall back to `auto-deny` — the safest default for a
    misconfigured deployment. (A noisy value silently widening to
    auto-approve would be a security footgun.)
    """
    raw = os.environ.get("CHARTER_STEPUP_APPROVAL_MODE", "").strip().lower()
    if raw in ("auto-approve", "auto-deny", "callback"):
        return raw
    return "auto-deny"


def _stepup_check_rate_limit(principal_id: str, agent_id: str) -> bool:
    """Return True iff the (principal_id, agent_id) is allowed another request.

    Token bucket: at most `_STEPUP_RATE_MAX` requests per principal_id +
    agent_id pair within the trailing `_STEPUP_RATE_WINDOW_SEC` window.
    Allowing requests records the timestamp; failing requests do NOT
    consume a slot (only successful checks do).

    This is a single-process bucket — `fly.io` multi-instance deployments
    will see independent buckets per instance until a backing store
    (Redis / fly-replay) lands (future work).
    """
    now = time.monotonic()
    cutoff = now - _STEPUP_RATE_WINDOW_SEC
    key = (principal_id, agent_id)
    with _stepup_rate_lock:
        bucket = _stepup_rate_state.setdefault(key, deque())
        # Drop expired timestamps from the LEFT (oldest first).
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= _STEPUP_RATE_MAX:
            return False
        bucket.append(now)
        return True


def _stepup_reset_rate_limit() -> None:
    """Test-only: clear the in-memory rate bucket.

    Exposed so test cases can reset the bucket between scenarios without
    monkeypatching module-level state. NOT called by production code
    paths (the bucket is intentionally process-lived).
    """
    with _stepup_rate_lock:
        _stepup_rate_state.clear()


def _stepup_validate_charter_url(url: str) -> str:
    """Light validation of the charter_url argument. Returns the trimmed URL.

    Empty / whitespace-only URLs raise `HTTPException(400)`. Anything
    else passes through — fetch-time errors (`CharterNotFoundError` /
    `CharterSchemaError`) surface from `_fetch_and_verify` and become
    400s upstream.
    """
    if not isinstance(url, str):
        raise HTTPException(status_code=400, detail="charter_url must be a string")
    trimmed = url.strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="charter_url must be non-empty")
    return trimmed


def _stepup_build_grant(
    *,
    request_body: AdHocGrantRequest,
    issuer_kid: str,
) -> AdHocGrant:
    """Build an unsigned AdHocGrant from a validated request.

    `grant_id` is a fresh UUID v4. `granted_at` is now (UTC, microsecond
    cleared for determinism). `expires_at = granted_at + max_ttl_seconds`.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    return AdHocGrant(
        grant_id=uuid.uuid4().hex,
        charter_url=request_body.charter_url,
        task=request_body.task,
        justification=request_body.justification,
        granted_at=now,
        expires_at=now + timedelta(seconds=request_body.max_ttl_seconds),
        issuer_kid=issuer_kid,
    )


def _stepup_callback_response(req: AdHocGrantRequest) -> AdHocGrantResponse:
    """Synchronously POST the request to `CHARTER_STEPUP_CALLBACK_URL`.

    The callback target is expected to return JSON matching
    `AdHocGrantResponse`. Failures (URL unset, network error, schema
    mismatch) collapse to a denied response with `denial_reason`
    describing the failure mode so the caller can diagnose without
    seeing a 500. This is a deliberate fail-closed choice: a misbehaving
    callback target should NEVER widen authority.

    The callback target is responsible for any approval UI (Slack / web
    form / etc.); this server only routes the request and trusts the
    callback's signed response by reference.
    """
    url = os.environ.get("CHARTER_STEPUP_CALLBACK_URL", "").strip()
    if not url:
        return AdHocGrantResponse(
            status="denied",
            grant=None,
            denial_reason=("approval mode is callback but CHARTER_STEPUP_CALLBACK_URL is not set"),
        )
    try:
        resp = httpx.post(url, json=req.model_dump(mode="json"), timeout=10.0)
        resp.raise_for_status()
        return AdHocGrantResponse.model_validate(resp.json())
    except Exception as e:
        return AdHocGrantResponse(
            status="denied",
            grant=None,
            denial_reason=f"callback failed: {type(e).__name__}: {e}",
        )


@app.post("/step-up", response_model=AdHocGrantResponse)
def step_up(request_body: AdHocGrantRequest) -> AdHocGrantResponse:
    """Request a short-TTL AdHocGrant that widens authority for one task.

    Validation order:

      1. Validate `charter_url` (non-empty string).
      2. Cap `max_ttl_seconds` at `CHARTER_STEPUP_MAX_TTL` (env, default
         3600s). Higher values return 400.
      3. Fetch + verify the Charter (404 / signature failures bubble
         up as 400 — the caller asked for a grant on a Charter we
         cannot verify, so it is unauthorisable).
      4. Apply rate limit (`(principal_id, agent_id)` -> 5 / 60s). 6th
         attempt within window returns 429.
      5. Dispatch to approval mode (`auto-deny` / `auto-approve` /
         `callback`).
      6. On approval: build, sign, persist the grant. Return
         `AdHocGrantResponse{status="approved", grant=<...>}`.

    The grant is signed with the issuer's private key, which is loaded
    from `data/keys/<principal_id>.pem` via `ensure_issuer_key`. This
    is the same key the Charter was signed with — ADR-002 (Ed25519
    only) extends to grants.
    """
    # 1 + 2: validate inputs.
    charter_url = _stepup_validate_charter_url(request_body.charter_url)
    cap = _stepup_max_ttl()
    if request_body.max_ttl_seconds > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                f"max_ttl_seconds={request_body.max_ttl_seconds} exceeds server cap "
                f"CHARTER_STEPUP_MAX_TTL={cap}"
            ),
        )

    # 3: fetch + verify Charter. Imported lazily to dodge the cyclical
    # mcp_server -> server import order at module load.
    from .mcp_server import _fetch_and_verify

    try:
        charter = _fetch_and_verify(charter_url)
    except CharterError as e:
        raise HTTPException(
            status_code=400,
            detail=f"charter_url could not be verified: {type(e).__name__}: {e}",
        ) from e

    # 4: rate limit on the verified binding.
    principal_id = charter.binding.principal_id
    agent_id = charter.binding.agent_id
    if not _stepup_check_rate_limit(principal_id, agent_id):
        raise HTTPException(
            status_code=429,
            detail=(
                f"step-up rate limit exceeded for ({principal_id!r}, {agent_id!r}): "
                f"max {_STEPUP_RATE_MAX} per {_STEPUP_RATE_WINDOW_SEC:.0f}s"
            ),
        )

    # 5: dispatch on approval mode. `auto-deny` short-circuits BEFORE
    # we touch the key store — production-safe default.
    mode = _stepup_approval_mode()
    if mode == "auto-deny":
        return AdHocGrantResponse(
            status="denied",
            grant=None,
            denial_reason="approval mode is auto-deny",
        )

    if mode == "callback":
        # Build a request payload AFTER capping the TTL so the callback
        # target sees the value we will actually sign on approval.
        capped_request = AdHocGrantRequest(
            charter_url=charter_url,
            task=request_body.task,
            justification=request_body.justification,
            max_ttl_seconds=request_body.max_ttl_seconds,
        )
        cb_response = _stepup_callback_response(capped_request)
        if cb_response.status != "approved":
            return cb_response
        # Callback approved — fall through to the signing path. If the
        # callback handed us a pre-built grant, we use it; otherwise we
        # build one ourselves.
        if cb_response.grant is not None:
            return cb_response
        # No grant supplied; treat as a green-light to mint one.

    # mode == "auto-approve" OR callback approved-with-no-grant.
    issuer_kid = charter.provenance.issuer_kid or ""
    if not issuer_kid:
        # Legacy Charter with no kid — refuse rather than mint a grant
        # we cannot route to a JWKS entry. This protects pre-v0.8
        # Charters from accidentally getting step-up grants tied to no
        # discoverable key.
        return AdHocGrantResponse(
            status="denied",
            grant=None,
            denial_reason="Charter has no issuer_kid; cannot mint step-up grant",
        )
    grant = _stepup_build_grant(request_body=request_body, issuer_kid=issuer_kid)
    private_key = ensure_issuer_key(principal_id)
    sign_grant(grant, private_key)
    _save_grant(grant)
    return AdHocGrantResponse(status="approved", grant=grant, denial_reason=None)


@app.get("/grants/{grant_id}")
def get_grant(grant_id: str) -> JSONResponse:
    """Return a stored AdHocGrant as JSON.

    Failure modes:
      404 — grant does not exist OR signature did not verify (we collapse
            these into one status code so an attacker probing grant_ids
            cannot tell "valid id, missing" from "invalid signature").
      410 — grant exists and signature is valid but `expires_at` is in
            the past. The 410 is a Gone-not-coming-back signal: by
            design step-up grants are not renewed (callers must
            `request_step_up` again to get a fresh one).

    To verify the signature we need the issuer's public key. The
    canonical source of truth is the Charter's
    `provenance.issuer_public_key`. We fetch the Charter via
    `_fetch_and_verify`, which applies the full JWKS / pin /
    lifecycle pipeline — if the underlying Charter is broken or
    revoked we can no longer prove the grant's authenticity, so we
    return 404 (treat as not-a-valid-grant). Charter expiry / revoke
    do NOT directly invalidate live grants, but they DO mean we have
    no key to verify the grant against, so the surface behaviour is
    the same: 404.
    """
    # We need the issuer key. Read the grant's charter_url to fetch
    # the Charter so we can pull its public key. The grant is loaded
    # first (without verifying signature) only to read charter_url —
    # then we re-load WITH verification once we have the key.
    from .mcp_server import _fetch_and_verify

    try:
        path = _grant_path_for_id(grant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="grant not found") from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="grant not found")
    raw = AdHocGrant.model_validate_json(path.read_text(encoding="utf-8"))

    try:
        charter = _fetch_and_verify(raw.charter_url)
    except CharterError:
        # We cannot prove authenticity without the Charter; treat
        # identically to "grant not found".
        raise HTTPException(status_code=404, detail="grant not found") from None

    try:
        grant = _load_grant(grant_id, charter.provenance.issuer_public_key)
    except CharterGrantNotFoundError as e:
        raise HTTPException(status_code=404, detail="grant not found") from e
    except CharterGrantSignatureError as e:
        # Surface as 404 — same reason as the comment above.
        raise HTTPException(status_code=404, detail="grant not found") from e
    except CharterGrantExpiredError as e:
        raise HTTPException(status_code=410, detail="grant expired") from e

    return JSONResponse(content=grant.model_dump(mode="json"))


def _grant_path_for_id(grant_id: str) -> Path:
    """Indirection so the server module does not import grants.grant_path
    at top level (avoiding a cycle if other modules import server)."""
    from .grants import grant_path

    return grant_path(grant_id)


# ---------------------------------------------------------------------------
# Inspector Web UI (B3.8)
#
# IMPORTANT: declared BEFORE the catch-all `/{principal_id}/{agent_id}` route
# so FastAPI routes `/inspect` and `/inspect/{p}/{a}` to the inspector. Path
# specificity in starlette is "declaration order wins among matches", and
# `/inspect/{p}/{a}` matches the same shape as the catch-all if put after.
# ---------------------------------------------------------------------------


def _inspector_unavailable_response() -> HTMLResponse:
    """503 body when `pip install charter[inspector]` was never run.

    Plain HTML (not JSON) so a browser visiting `/inspect?url=...` on a
    deployment without the extra still sees a readable error page that
    tells the operator exactly which command to run.
    """
    return HTMLResponse(
        status_code=503,
        content=(
            "<!doctype html><html><body>"
            "<h1>Charter Inspector unavailable</h1>"
            "<p>The <code>jinja2</code> dependency is not installed on this "
            "server. Install with:</p>"
            "<pre>pip install charter[inspector]</pre>"
            "</body></html>"
        ),
    )


def _do_inspect(charter_url: str) -> HTMLResponse:
    """Shared implementation of both inspect routes.

    Trust path: every URL we touch (leaf + chain parents) is
    `_fetch_and_verify`-d, so JWKS / pin / lifecycle checks run on
    every hop. Verification FAILURES still produce a renderable page
    (red banner + clauses) because the issuer needs to be able to SEE
    why their Charter rejected, not get a faceless JSON 500.

    Per the AC, charter_url is sanitised through allowlist URL
    validation (`scheme in {http, https}`, RFC 1918 SSRF guard) before
    any network call. Validation failures yield a styled 400 page.
    """
    from . import inspector as _inspector
    from .mcp_server import _fetch_and_verify

    if not _inspector.is_available():
        return _inspector_unavailable_response()

    # 1. Validate the URL (allowlist + SSRF guard).
    try:
        normalized = _inspector.validate_charter_url(charter_url)
    except _inspector.InvalidCharterURLError as e:
        return HTMLResponse(
            status_code=400,
            content=_inspector.render_invalid_url(charter_url, str(e)),
        )

    # 2. Fetch + verify. Different error classes feed into the page
    #    in two different ways:
    #    - "no Charter could be parsed at all" (NotFound, Schema) -> error.html
    #    - "Charter parsed but verification failed" (Signature, Pin, JWKS,
    #      Revoked, Expired) -> render inspect.html with a red banner so the
    #      issuer can still see clauses.
    charter = None
    fetch_error: CharterError | None = None
    try:
        charter = _fetch_and_verify(normalized)
    except CharterSchemaError as e:
        return HTMLResponse(
            status_code=502,
            content=_inspector.render_fetch_error(normalized, e),
        )
    except CharterError as e:
        # Try one more time WITHOUT verification so we can still show the
        # clauses behind the red banner. We refuse to follow this path for
        # NotFound (no body to parse) and Schema (body unparseable).
        fetch_error = e
        try:
            import httpx

            from .schema import Charter as _Charter

            resp = httpx.get(normalized, timeout=10.0)
            resp.raise_for_status()
            charter = _Charter.model_validate(resp.json())
        except Exception:
            # Couldn't even retrieve a parseable body — fall through to
            # the generic error page.
            return HTMLResponse(
                status_code=502,
                content=_inspector.render_fetch_error(normalized, e),
            )

    if charter is None:  # defensive: should be unreachable
        return HTMLResponse(
            status_code=502,
            content=_inspector.render_fetch_error(
                normalized, RuntimeError("no charter was fetched")
            ),
        )

    # 3. Walk the chain (best-effort; broken hops render as broken nodes).
    chain = _inspector.walk_chain(charter, normalized, fetcher=_fetch_and_verify)

    # 4. Diff vs prior (lifecycle.replaces), if any.
    diff_text = _inspector.diff_against_prior(charter)

    # 5. Render. 200 even when fetch_error is set, because the page IS
    #    correctly served — the banner inside it tells the operator the
    #    Charter itself is not to be trusted. Returning 5xx here would
    #    make browsers display their default error chrome and hide the
    #    body that contains the diagnostic information.
    status_code = 200 if fetch_error is None else 200
    if isinstance(fetch_error, CharterSignatureError):
        # Surface signature failures as a distinct status so a CI scraper
        # can alert on them without parsing HTML. 409 is the closest
        # match: "the request was valid but the resource's state
        # (signature) conflicts with what we'd accept."
        status_code = 409
    return HTMLResponse(
        status_code=status_code,
        content=_inspector.render_charter(
            charter,
            normalized,
            chain=chain,
            diff_text=diff_text,
            fetch_error=fetch_error,
        ),
    )


@app.get("/inspect")
def inspect_by_url(
    url: str = Query(..., description="Charter URL to inspect (http or https)"),
) -> HTMLResponse:
    """Render a charter at an arbitrary URL as a human-readable page.

    The URL is allowlist-validated (scheme in {http, https}, no RFC
    1918 / loopback by default) and then fetched through the same
    `_fetch_and_verify` path as the MCP tool, so JWKS / pin /
    lifecycle / signature checks all apply.

    Set `CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS=1` to permit `localhost`
    / `127.0.0.1` targets — useful for local-dev where the inspector
    and the charter server share a host.
    """
    return _do_inspect(url)


@app.get("/inspect/{principal_id}/{agent_id}")
def inspect_by_binding(principal_id: str, agent_id: str) -> HTMLResponse:
    """Convenience route: inspect the charter at the local
    `{CHARTER_URL_BASE}/{principal_id}/{agent_id}` URL.

    Equivalent to calling `/inspect?url=<CHARTER_URL_BASE>/<p>/<a>`. The
    fully-qualified URL is constructed server-side from `CHARTER_URL_BASE`
    so the caller doesn't have to type it out, then handed to the same
    `_do_inspect` pipeline as the query-string route — every safety
    check (allowlist, SSRF guard, signature, pin, lifecycle) runs
    identically.
    """
    base = os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE).rstrip("/")
    return _do_inspect(f"{base}/{principal_id}/{agent_id}")


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
