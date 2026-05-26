"""Storage for AdHocGrants (B2.5 step-up protocol, ADR-013).

Layout under `data/`:

    data/
      grants/
        <grant_id>.json          one grant per file

Grants are short-TTL signed tokens issued by `POST /step-up`. They are
NOT transparency-logged in v0.9 (ADR-013 future-work). They have no
`revoked` lifecycle state — early teardown is physical deletion of the
file (`revoke_grant`).

Path-safety: every input that becomes a filesystem segment goes through
`charter.storage._safe()`, the same allowlist used by Charter / Disclosure
storage (Batch 1 Priv-1 path-traversal lesson). The boundary check is
belt-and-suspenders: after sanitising the `grant_id`, the resolved path
must still live under `data/grants/`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ._logging import get_logger
from .errors import (
    CharterGrantExpiredError,
    CharterGrantNotFoundError,
    CharterGrantSignatureError,
)
from .schema import AdHocGrant
from .signing import verify_grant_signature
from .storage import _safe, data_root

_log = get_logger("charter.grants")


def grants_dir() -> Path:
    """`data/grants/` — created on demand."""
    d = data_root() / "grants"
    d.mkdir(parents=True, exist_ok=True)
    return d


def grant_path(grant_id: str) -> Path:
    """Per-grant JSON file path.

    `grant_id` is treated as untrusted HTTP path input (the FastAPI
    route exposes it directly via `GET /grants/{grant_id}`). Defense
    in depth:

      1. `_safe()` strips every char outside the `[A-Za-z0-9._-]`
         allowlist (covers `..`, `/`, `\\`, null bytes, percent-decoded
         path separators, etc.).
      2. After joining, the resolved path must live under
         `grants_dir()`. `resolve()` collapses any residual `..`
         segments; `is_relative_to` is the 3.9+ replacement for the
         older try/except idiom on `relative_to`.

    Caller (`load_grant` / server endpoint) catches `ValueError` and
    translates to the same 404 every other failure mode returns, so
    attackers cannot use response shape to distinguish "valid id,
    missing file" from "traversal blocked".
    """
    root = grants_dir()
    candidate = root / f"{_safe(grant_id)}.json"
    root_resolved = root.resolve()
    if not candidate.resolve().is_relative_to(root_resolved):
        raise ValueError(f"grant path escapes grants dir: grant_id={grant_id!r}")
    return candidate


def save_grant(grant: AdHocGrant) -> Path:
    """Persist a signed grant to `data/grants/<grant_id>.json`.

    Caller is responsible for having signed the grant first
    (`sign_grant`); we do NOT re-sign here. Returns the path written.
    """
    path = grant_path(grant.grant_id)
    path.write_text(grant.model_dump_json(indent=2), encoding="utf-8")
    _log.info(
        "grant saved",
        extra={
            "grant_id": grant.grant_id,
            "charter_url": grant.charter_url,
            "expires_at": grant.expires_at.isoformat(),
            "outcome": "saved",
        },
    )
    return path


def load_grant(grant_id: str, issuer_public_key_str: str) -> AdHocGrant:
    """Load a grant by id; verify signature; verify it is not expired.

    Raises:
        CharterGrantNotFoundError: file does not exist on disk, or the
            id is rejected by `_safe()` (empty / traversal).
        CharterGrantSignatureError: signature does not verify against
            the supplied issuer public key.
        CharterGrantExpiredError: `expires_at` is in the past.

    The issuer key must be supplied by the caller (typically fetched
    from the linked Charter's `provenance.issuer_public_key`).
    """
    try:
        path = grant_path(grant_id)
    except ValueError as e:
        raise CharterGrantNotFoundError(str(e)) from e
    if not path.exists():
        raise CharterGrantNotFoundError(f"no grant at {path}")
    grant = AdHocGrant.model_validate_json(path.read_text(encoding="utf-8"))

    if not verify_grant_signature(grant, issuer_public_key_str):
        raise CharterGrantSignatureError(
            f"grant {grant_id!r} signature did not verify against supplied issuer key"
        )

    now = datetime.now(UTC)
    if grant.expires_at <= now:
        raise CharterGrantExpiredError(
            f"grant {grant_id!r} expired at {grant.expires_at.isoformat()}"
        )

    return grant


def verify_grant(grant: AdHocGrant, issuer_public_key_str: str) -> bool:
    """Convenience wrapper: True iff signature is valid AND not expired.

    Does NOT raise — returns False on any failure mode. Use `load_grant`
    if you need to distinguish "missing" vs "expired" vs "bad signature".
    """
    if not verify_grant_signature(grant, issuer_public_key_str):
        return False
    return grant.expires_at > datetime.now(UTC)


def revoke_grant(grant_id: str) -> bool:
    """Physically delete a grant file. No `revoked` lifecycle state.

    Returns True if a file was deleted, False if it did not exist. The
    operation is idempotent.
    """
    try:
        path = grant_path(grant_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    path.unlink()
    _log.info(
        "grant revoked",
        extra={"grant_id": grant_id, "outcome": "revoked"},
    )
    return True


__all__ = [
    "grants_dir",
    "grant_path",
    "save_grant",
    "load_grant",
    "verify_grant",
    "revoke_grant",
]
