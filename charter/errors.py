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


__all__ = [
    "CharterError",
    "CharterNotFoundError",
    "CharterSchemaError",
    "CharterSignatureError",
    "CharterExpiredError",
    "CharterRevokedError",
]
