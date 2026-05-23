"""JWKS client with TTL cache (v0.8 trust-model upgrade).

Charters issued from v0.8 onwards carry a `provenance.issuer_kid` so verifiers
can look up the signing key from the issuer's `.well-known/jwks.json`
endpoint instead of trusting the inline `issuer_public_key` blindly. This is
what lets old callers detect key rotation without needing a new Charter, and
what lets a fresh caller catch a Charter whose `issuer_public_key` was forged
to match an `issuer_id` it doesn't actually control.

Cache: per-process in-memory, keyed by issuer origin (`scheme://netloc`).
TTL defaults to 5 minutes (`_DEFAULT_TTL_SECONDS`), overridable via the
`CHARTER_JWKS_CACHE_TTL` env var.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Final
from urllib.parse import urlparse

import httpx

from ._logging import get_logger
from .errors import JWKSNotFoundError, JWKSParseError
from .observability import charter_span_cm, set_span_attrs

_log = get_logger("charter.keys")

_DEFAULT_TTL_SECONDS: Final[int] = 300

# {issuer_origin: (fetched_at_monotonic, {kid: jwk_dict})}
_cache: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}


def _cache_ttl_seconds() -> float:
    raw = os.environ.get("CHARTER_JWKS_CACHE_TTL", "").strip()
    if not raw:
        return float(_DEFAULT_TTL_SECONDS)
    try:
        return float(raw)
    except ValueError:
        return float(_DEFAULT_TTL_SECONDS)


def issuer_origin_from_url(url: str) -> str:
    """Return `{scheme}://{netloc}` for an arbitrary HTTP URL.

    Used to derive an issuer's JWKS endpoint from any Charter URL on
    that same host.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Cannot derive issuer origin from {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def fetch_jwks(issuer_origin: str) -> dict[str, dict[str, str]]:
    """Fetch + cache the JWKS for an issuer origin. Keyed by `kid`.

    Args:
        issuer_origin: `{scheme}://{netloc}` (no trailing slash needed).

    Returns:
        Mapping of `kid -> JWK dict` for every key the issuer publishes.

    Raises:
        JWKSNotFoundError: network failure or non-2xx HTTP response.
        JWKSParseError:    body is not a valid JWKS document.

    Emits one `charter.fetch_jwks` OTel span per call with
    `charter.jwks_cache_hit` and `charter.jwks_key_count` attributes
    when OTel is installed.
    """
    origin = issuer_origin.rstrip("/")

    with charter_span_cm(
        "charter.fetch_jwks",
        {"charter.issuer_origin": origin},
    ) as span:
        cached = _cache.get(origin)
        if cached is not None:
            fetched_at, keys = cached
            if time.monotonic() - fetched_at < _cache_ttl_seconds():
                _log.debug("jwks cache hit", extra={"origin": origin, "outcome": "cache_hit"})
                set_span_attrs(
                    span,
                    {
                        "charter.jwks_cache_hit": True,
                        "charter.jwks_key_count": len(keys),
                        "charter.verdict": "ok",
                    },
                )
                return keys

        set_span_attrs(span, {"charter.jwks_cache_hit": False})
        keys_fetched = _fetch_jwks_uncached(origin)
        set_span_attrs(
            span,
            {
                "charter.jwks_key_count": len(keys_fetched),
                "charter.verdict": "ok",
            },
        )
        return keys_fetched


def _fetch_jwks_uncached(origin: str) -> dict[str, dict[str, str]]:
    """The original network + parse path, kept verbatim. Splitting it out
    keeps the span wrapper above readable; semantics are unchanged."""
    url = f"{origin}/.well-known/jwks.json"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        _log.warning(
            "jwks fetch failed: HTTP error",
            extra={
                "url": url,
                "status_code": e.response.status_code,
                "outcome": "not_found",
            },
        )
        raise JWKSNotFoundError(f"GET {url} -> HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        _log.warning(
            "jwks fetch failed: request error",
            extra={"url": url, "error": str(e), "outcome": "not_found"},
        )
        raise JWKSNotFoundError(f"GET {url} failed: {e}") from e

    try:
        data = resp.json()
        keys_list = data["keys"]
        if not isinstance(keys_list, list):
            raise ValueError("'keys' must be a list")
        result: dict[str, dict[str, str]] = {}
        for entry in keys_list:
            if not isinstance(entry, dict):
                raise ValueError("each entry must be a dict")
            kid = entry.get("kid")
            if not isinstance(kid, str):
                raise ValueError("each entry must have a 'kid' string")
            result[kid] = entry
    except (ValueError, KeyError, TypeError) as e:
        _log.warning(
            "jwks fetch failed: parse error",
            extra={"url": url, "error": str(e), "outcome": "parse_error"},
        )
        raise JWKSParseError(f"Invalid JWKS at {url}: {e}") from e

    _cache[origin] = (time.monotonic(), result)
    _log.info(
        "jwks fetched",
        extra={"url": url, "key_count": len(result), "outcome": "ok"},
    )
    return result


def jwk_to_public_key_string(jwk: dict[str, str]) -> str:
    """Convert a JWK dict back to `ed25519:<base64>` form.

    Used to compare a JWKS-published key against a Charter's inline
    `issuer_public_key`. Validates `kty=OKP` and `crv=Ed25519`.

    Raises:
        ValueError: JWK is not an Ed25519 OKP key, or `x` is missing.
    """
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError(f"Unsupported JWK: kty={jwk.get('kty')!r} crv={jwk.get('crv')!r}")
    x = jwk.get("x")
    if not isinstance(x, str):
        raise ValueError("JWK 'x' field missing or not a string")
    # base64url decode (re-pad if the encoder stripped `=`)
    padding = "=" * (-len(x) % 4)
    raw = base64.urlsafe_b64decode(x + padding)
    return f"ed25519:{base64.b64encode(raw).decode('ascii')}"


def clear_cache() -> None:
    """Drop all cached JWKS entries. Test helper."""
    _cache.clear()
