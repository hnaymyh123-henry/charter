"""Typed exceptions for the Charter protocol.

The MCP server raises these when fetch + verify fails so SDK consumers can
catch on specific failure modes instead of pattern-matching on `ValueError`
prefix strings.

Mapping from failure mode to Compatibility Check outcome (per the spec
§ Charter Lifecycle):

    CharterNotFoundError    -> caller treats as `incompatible`
    CharterSchemaError      -> caller treats as `incompatible`
    CharterSignatureError   -> caller treats as `incompatible`
    CharterRevokedError     -> caller treats as `incompatible`
    CharterExpiredError     -> caller treats as `needs_approval`
"""

from __future__ import annotations


class CharterError(Exception):
    """Base class for all Charter protocol errors.

    Catch this if you want a single try/except that handles every charter-
    layer failure. Catch a specific subclass when the failure mode changes
    your behavior.
    """


class CharterNotFoundError(CharterError):
    """The charter_url returned 404 or could not be reached at all."""


class CharterSchemaError(CharterError):
    """The fetched body could not be parsed as a Charter (bad JSON, schema
    mismatch, missing required field)."""


class CharterSignatureError(CharterError):
    """The Charter's `provenance.issuer_signature` did not verify against
    its embedded `provenance.issuer_public_key`."""


class CharterExpiredError(CharterError):
    """The Charter has expired (`status=expired`) or been superseded by a
    newer Charter (`status=superseded`). Callers should fall back to
    `needs_approval` and prompt for a fresh Charter."""


class CharterRevokedError(CharterError):
    """The Charter has been explicitly revoked by its issuer
    (`status=revoked`). Callers should treat as `incompatible` and refuse
    delegation."""


class CharterKeyMismatchError(CharterError):
    """A v0.8+ Charter's `issuer_kid` points at a key in the issuer's
    JWKS that does NOT match the embedded `issuer_public_key`, OR the
    `issuer_kid` is not listed in the JWKS at all.

    Both cases mean the issuer's published key directory disagrees
    with what the Charter claims signed it — either the Charter is a
    forgery, or the issuer is lying about which key signed it.
    Callers should treat as `incompatible`."""


class CharterPinMismatchError(CharterError):
    """A Charter's verifying key does NOT match the previously-pinned
    fingerprint for this principal. This is the layer that catches a
    host compromise where the attacker now signs with their own key
    and rotated the JWKS at the same time. Callers should treat as
    `incompatible`. After a legitimate key rotation the operator must
    run `charter pins reset <principal>` to drop the pin."""


class JWKSNotFoundError(CharterError):
    """The issuer's `.well-known/jwks.json` endpoint could not be
    reached (404, 5xx, or network error). Raised by `fetch_jwks`."""


class JWKSParseError(CharterError):
    """The body returned by `.well-known/jwks.json` was not a valid
    JWKS document. Raised by `fetch_jwks`."""


__all__ = [
    "CharterError",
    "CharterNotFoundError",
    "CharterSchemaError",
    "CharterSignatureError",
    "CharterExpiredError",
    "CharterRevokedError",
    "CharterKeyMismatchError",
    "CharterPinMismatchError",
    "JWKSNotFoundError",
    "JWKSParseError",
]
