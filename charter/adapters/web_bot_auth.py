"""Web Bot Auth signed-header adapter — RFC 9421 Charter integration.

The point of this adapter is to push the Charter Compatibility Check
out of the calling-agent process and onto the network edge. A calling
agent signs every outbound HTTP request with its Ed25519 key, embeds
``charter_url`` as a custom signature parameter, and any cooperating
hop (Cloudflare edge, enterprise egress proxy, or the target service
itself) can:

  1. Verify the signature against the issuer's published JWKS.
  2. Extract ``charter_url`` from the Signature-Input header.
  3. Fetch + verify the Charter using ``charter._fetch_and_verify`` —
     same code path the calling agent uses, same trust order
     (signature → JWKS → pin → lifecycle, per ADR-007).
  4. If the Charter's verdict for any request is not ``allow``, return
     403 with a structured body explaining which clauses applied.

The adapter intentionally implements only the minimal RFC 9421 subset
the demo needs (Ed25519, four covered components, custom string
parameter). See :mod:`charter.adapters._rfc9421` for the coverage
statement. If you need full RFC 9421 / 9530 support, swap that
module out for ``http-message-signatures``.

Three entry points:

  - :func:`sign_request` — build Signature / Signature-Input headers.
  - :func:`verify_request` — verify a received request and return a
    structured :class:`WebBotAuthResult`.
  - :func:`gated_middleware` — FastAPI/Starlette ASGI middleware
    factory that wires verification + Charter fetch + verdict
    aggregation together. Verdicts other than ``allow`` short-circuit
    the request with a 403 + JSON body.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .._logging import get_logger
from ..errors import CharterError
from ..keys import (
    fetch_jwks as default_fetch_jwks,
)
from ..keys import (
    issuer_origin_from_url,
    jwk_to_public_key_string,
)
from ..mcp_server import _fetch_and_verify as default_fetch_charter
from ..schema import Charter
from ..signing import public_key_from_string
from . import _rfc9421

_log = get_logger("charter.adapters.web_bot_auth")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebBotAuthResult:
    """Outcome of :func:`verify_request`.

    ``signature_ok`` is True iff the signature verified against the
    public key resolved from ``key_id``. ``charter_url`` and ``key_id``
    are populated whenever the Signature-Input header parsed
    successfully — they may both be set even when ``signature_ok=False``
    (e.g. body tampering produced a bad content-digest match).
    ``reason`` is always a short human-readable string suitable for
    logging or 4xx response bodies.
    """

    signature_ok: bool
    charter_url: str | None
    key_id: str | None
    reason: str


class JWKSFetcher(Protocol):
    """Type for the JWKS fetcher hook (mostly for tests)."""

    def __call__(self, issuer_origin: str) -> dict[str, dict[str, str]]: ...


class CharterFetcher(Protocol):
    """Type for the Charter fetch + verify hook (mostly for tests)."""

    def __call__(self, charter_url: str) -> Charter: ...


# ---------------------------------------------------------------------------
# sign_request / verify_request
# ---------------------------------------------------------------------------


def sign_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    charter_url: str,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> dict[str, str]:
    """Return a NEW headers dict with Signature + Signature-Input + Content-Digest.

    Caller is responsible for sending the returned headers with the
    request. The original ``headers`` dict is not mutated.

    The signature covers ``@method``, ``@path``, ``@authority``,
    ``content-digest`` (only when ``body`` is non-empty), and the custom
    ``charter_url`` signature parameter. Verifiers must reconstruct the
    same component list to recompute the signature base.
    """
    if not charter_url:
        raise ValueError("charter_url is required for Web Bot Auth signing")
    added = _rfc9421.sign(
        method=method,
        url=url,
        headers=headers,
        body=body,
        charter_url=charter_url,
        private_key=private_key,
        key_id=key_id,
    )
    merged = dict(headers)
    merged.update(added)
    return merged


def verify_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    fetch_jwks_fn: JWKSFetcher | None = None,
) -> WebBotAuthResult:
    """Verify a Signature header against the issuer's JWKS.

    Pulls ``keyid`` and ``charter_url`` out of Signature-Input, resolves
    the public key by deriving the JWKS endpoint from ``charter_url``,
    and verifies. Body integrity is enforced through the content-digest
    binding (see :mod:`_rfc9421`).

    The fetcher hook lets tests substitute a deterministic JWKS — by
    default we go through :func:`charter.keys.fetch_jwks`.
    """
    fetch_jwks = fetch_jwks_fn or default_fetch_jwks

    # Headers may arrive case-mixed; normalize once.
    headers_lower = {k.lower(): v for k, v in headers.items()}

    sig_input_raw = headers_lower.get("signature-input")
    sig_raw = headers_lower.get("signature")
    if not sig_input_raw or not sig_raw:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=None,
            key_id=None,
            reason="missing Signature or Signature-Input header",
        )

    try:
        _, covered, params = _rfc9421.parse_signature_input(sig_input_raw)
        params_value = _rfc9421.extract_signature_params_value(sig_input_raw)
        _, signature_bytes = _rfc9421.parse_signature_header(sig_raw)
    except _rfc9421.SignatureInputParseError as e:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=None,
            key_id=None,
            reason=f"malformed Signature-Input / Signature header: {e}",
        )

    charter_url = params.get("charter_url") or None
    key_id = params.get("keyid") or None
    alg = params.get("alg")

    if alg and alg != "ed25519":
        # ADR-002: Ed25519 only. We reject foreign algs even if the
        # underlying key could be reinterpreted, to avoid algorithm
        # confusion attacks.
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=key_id,
            reason=f"unsupported alg {alg!r}; Charter requires ed25519",
        )

    if charter_url is None:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=None,
            key_id=key_id,
            reason="Signature-Input is missing the charter_url parameter",
        )
    if key_id is None:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=None,
            reason="Signature-Input is missing the keyid parameter",
        )

    # Resolve issuer origin from the charter_url; both must be on the
    # same host as required by the Charter trust model.
    try:
        origin = issuer_origin_from_url(charter_url)
    except ValueError as e:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=key_id,
            reason=f"cannot derive issuer origin from charter_url: {e}",
        )

    try:
        jwks = fetch_jwks(origin)
    except CharterError as e:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=key_id,
            reason=f"JWKS lookup failed: {e}",
        )

    jwk = jwks.get(key_id)
    if jwk is None:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=key_id,
            reason=f"key_id {key_id!r} not present in JWKS at {origin}",
        )

    try:
        public_key_str = jwk_to_public_key_string(jwk)
        public_key: Ed25519PublicKey = public_key_from_string(public_key_str)
    except ValueError as e:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=key_id,
            reason=f"JWKS entry for {key_id!r} is not a valid Ed25519 key: {e}",
        )

    ok = _rfc9421.verify(
        method=method,
        url=url,
        headers=headers,
        body=body,
        signature_bytes=signature_bytes,
        covered_components=covered,
        signature_params_value=params_value,
        public_key=public_key,
    )
    if not ok:
        return WebBotAuthResult(
            signature_ok=False,
            charter_url=charter_url,
            key_id=key_id,
            reason="signature did not verify (key, body, or covered components mismatch)",
        )

    return WebBotAuthResult(
        signature_ok=True,
        charter_url=charter_url,
        key_id=key_id,
        reason="ok",
    )


# ---------------------------------------------------------------------------
# Verdict integration (re-using the calling-agent's grader contract)
# ---------------------------------------------------------------------------


HitsGrader = Callable[[Charter, str], list[dict[str, Any]]]


def _default_request_task(method: str, path: str) -> str:
    """Default intended-task text for incoming edge-verified requests.

    The middleware doesn't know what the calling agent's *intent* is —
    it only sees a method + path. We surface that as the task string so
    Charter graders that key on URL patterns (e.g. "is /admin allowed")
    can still produce useful per-clause hits. Callers that want richer
    semantics should pass their own ``task_from`` and ``hits_grader``.
    """
    return f"HTTP {method.upper()} {path}"


# ---------------------------------------------------------------------------
# Middleware factory
# ---------------------------------------------------------------------------


def gated_middleware(
    app: ASGIApp,
    *,
    fetch_charter_fn: CharterFetcher | None = None,
    fetch_jwks_fn: JWKSFetcher | None = None,
    hits_grader: HitsGrader | None = None,
    task_from: Callable[[Request], str] | None = None,
    cache_size: int = 256,
) -> _WebBotAuthMiddleware:
    """Return a Starlette/FastAPI middleware that gates every request on Charter verdict.

    Behaviour per incoming request:

      1. ``verify_request`` is invoked. On failure → 403 with
         ``{error_code: "web_bot_auth_signature_invalid", ...}``.
      2. Charter is fetched + verified via ``_fetch_and_verify`` (or a
         caller-provided substitute). LRU-cached by ``charter_url`` and
         honoured up to ``lifecycle.valid_until``. Failures → 403 with
         ``{error_code: "charter_unavailable", ...}``.
      3. Per-clause hits are produced by the supplied ``hits_grader``
         (default: no hits, which yields ``needs_approval`` from
         ``aggregate_verdict``).
      4. ``aggregate_verdict`` runs. ``decision != "allow"`` → 403 with
         ``{error_code, charter_url, decision, applied_clauses, ...}``.
      5. Otherwise the wrapped app handles the request normally.

    Args:
        app:               The ASGI app to wrap.
        fetch_charter_fn:  Test override for `_fetch_and_verify`.
        fetch_jwks_fn:     Test override for `fetch_jwks` passed into
                           `verify_request`.
        hits_grader:       Optional grader. Default produces an empty hit
                           list, which yields ``needs_approval`` and
                           blocks the request — operators should plug in
                           their own grader before relying on this in
                           production.
        task_from:         Optional callable mapping the incoming
                           Request to the intended-task string passed to
                           the grader.
        cache_size:        Max number of Charters to keep in the LRU.
    """
    return _WebBotAuthMiddleware(
        app,
        fetch_charter_fn=fetch_charter_fn,
        fetch_jwks_fn=fetch_jwks_fn,
        hits_grader=hits_grader,
        task_from=task_from,
        cache_size=cache_size,
    )


# A tiny LRU keyed by charter_url. Stores (Charter, valid_until).
class _CharterCache:
    """In-process LRU cache for verified Charters.

    Keyed by ``charter_url``. Entries expire when their Charter's
    ``lifecycle.valid_until`` has passed — we always honour that bound
    so a stale Charter can't outlast its declared TTL. A future
    iteration (B1.3) will layer Cache-Control honouring on top.
    """

    def __init__(self, max_size: int) -> None:
        self._max = max(1, max_size)
        self._data: dict[str, tuple[Charter, datetime]] = {}

    def get(self, key: str) -> Charter | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        charter, valid_until = entry
        if datetime.now(UTC) >= valid_until:
            self._data.pop(key, None)
            return None
        # Move-to-end semantics for LRU recency tracking.
        self._data.pop(key)
        self._data[key] = entry
        return charter

    def put(self, key: str, charter: Charter) -> None:
        self._data.pop(key, None)
        self._data[key] = (charter, charter.lifecycle.valid_until)
        while len(self._data) > self._max:
            # Drop the oldest insertion.
            oldest = next(iter(self._data))
            self._data.pop(oldest, None)


class _WebBotAuthMiddleware(BaseHTTPMiddleware):
    """Starlette/ASGI middleware implementing the gated_middleware factory."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        fetch_charter_fn: CharterFetcher | None,
        fetch_jwks_fn: JWKSFetcher | None,
        hits_grader: HitsGrader | None,
        task_from: Callable[[Request], str] | None,
        cache_size: int,
    ) -> None:
        super().__init__(app)
        self._fetch_charter = fetch_charter_fn or default_fetch_charter
        self._fetch_jwks = fetch_jwks_fn
        self._hits_grader = hits_grader
        self._task_from = task_from
        self._cache = _CharterCache(cache_size)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        body = await request.body()
        # Starlette consumes the body stream when we await request.body().
        # We need to put it back so downstream handlers can read it. The
        # attribute is private but stable; this is the idiomatic way to
        # share a buffered body across middleware in Starlette.
        request._body = body

        url = str(request.url)
        headers = dict(request.headers)
        method = request.method

        result = verify_request(
            method,
            url,
            headers,
            body,
            fetch_jwks_fn=self._fetch_jwks,
        )

        if not result.signature_ok:
            _log.warning(
                "web_bot_auth rejected request: signature invalid",
                extra={
                    "path": request.url.path,
                    "reason": result.reason,
                    "charter_url": result.charter_url,
                    "outcome": "signature_invalid",
                },
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error_code": "web_bot_auth_signature_invalid",
                    "reason": result.reason,
                    "charter_url": result.charter_url,
                    "applied_clauses": [],
                },
            )

        assert result.charter_url is not None  # guaranteed by signature_ok=True
        charter = self._cache.get(result.charter_url)
        if charter is None:
            try:
                charter = self._fetch_charter(result.charter_url)
            except CharterError as e:
                _log.warning(
                    "web_bot_auth rejected request: charter fetch failed",
                    extra={
                        "path": request.url.path,
                        "charter_url": result.charter_url,
                        "error": str(e),
                        "outcome": "charter_unavailable",
                    },
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "error_code": "charter_unavailable",
                        "reason": f"{type(e).__name__}: {e}",
                        "charter_url": result.charter_url,
                        "applied_clauses": [],
                    },
                )
            self._cache.put(result.charter_url, charter)

        intended_task = (
            self._task_from(request)
            if self._task_from is not None
            else _default_request_task(method, request.url.path)
        )

        hits = self._hits_grader(charter, intended_task) if self._hits_grader else []
        verdict = _aggregate_for_request(charter, hits)

        if verdict["decision"] != "allow":
            applied = [m["id"] for m in verdict.get("matched_clauses", []) if m.get("applied")]
            _log.info(
                "web_bot_auth rejected request: verdict not allow",
                extra={
                    "path": request.url.path,
                    "charter_url": result.charter_url,
                    "decision": verdict["decision"],
                    "applied": applied,
                    "outcome": "blocked",
                },
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error_code": (
                        "charter_needs_approval"
                        if verdict["decision"] == "needs_approval"
                        else "charter_incompatible"
                    ),
                    "reason": verdict.get("reason", ""),
                    "charter_url": result.charter_url,
                    "applied_clauses": applied,
                    "decision": verdict["decision"],
                },
            )

        return await call_next(request)


def _aggregate_for_request(charter: Charter, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Call ``aggregate_verdict`` without going through MCP's decorator wrapper.

    Mirrors the ``_call_aggregate_verdict`` helper in
    :mod:`charter.adapters.openai_agents`. We re-implement here to avoid
    cross-adapter imports.
    """
    from ..mcp_server import aggregate_verdict as _agg_tool

    payload = charter.model_dump(mode="json")
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(_agg_tool, attr):
            raw = getattr(_agg_tool, attr)(payload, hits)
            return cast(dict[str, Any], raw)
    return cast(dict[str, Any], _agg_tool(payload, hits))


# ---------------------------------------------------------------------------
# Convenience: attach to a FastAPI app
# ---------------------------------------------------------------------------


def install_on(app: FastAPI, **kwargs: Any) -> FastAPI:
    """Convenience helper: install gated_middleware on a FastAPI app.

    Equivalent to::

        app.add_middleware(BaseHTTPMiddleware, dispatch=gated_middleware(app, **kwargs).dispatch)

    but lets the caller write a single line. Returns the same app for
    chaining.
    """
    # Starlette's add_middleware constructs the middleware with `app`
    # injected, so we pass our class plus the keyword overrides.
    app.add_middleware(_WebBotAuthMiddleware, **kwargs)
    return app


__all__ = [
    "WebBotAuthResult",
    "HitsGrader",
    "gated_middleware",
    "install_on",
    "sign_request",
    "verify_request",
]
