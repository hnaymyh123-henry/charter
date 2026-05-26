"""Ed25519 keygen / sign / verify for Self-Attesting Charters.

A Charter is signed by the issuer's Ed25519 private key. The corresponding
public key is embedded inline at `provenance.issuer_public_key`, so anyone
fetching the Charter via HTTPS can verify the signature using only the JSON
itself.

Signing covers a canonical serialization of the Charter with the
`provenance.issuer_signature` field cleared. This avoids the chicken-and-egg
of signing a payload that contains its own signature.

Trust root: HTTPS to the charter_url host (TOFU on first fetch).

## Private key storage

Private keys are stored under `data/keys/<principal>.pem`. If
`CHARTER_KEY_PASSPHRASE` is set the PEM is encrypted with
`cryptography.serialization.BestAvailableEncryption`; otherwise it is
written in plaintext AND a loud WARN log is emitted on every write so
the failure mode is hard to miss in production.

The loader detects encrypted vs plaintext from the PEM header
(`-----BEGIN ENCRYPTED PRIVATE KEY-----` vs `-----BEGIN PRIVATE KEY-----`)
and behaves accordingly:

  - Encrypted on disk + passphrase configured → decrypted load.
  - Encrypted on disk + no passphrase         → ValueError.
  - Plaintext on disk                         → loaded; if a
    passphrase is also configured we emit an INFO log telling the
    deployer to re-save under the passphrase to opt into encryption.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ._logging import get_logger
from .schema import AdHocGrant, Charter

_log = get_logger("charter.signing")

# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Create a fresh Ed25519 keypair for an issuer."""
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


def _key_passphrase() -> bytes | None:
    """Return the configured passphrase (UTF-8 bytes) or None.

    Empty / unset env var -> None. Trims whitespace defensively so
    `CHARTER_KEY_PASSPHRASE=" "` doesn't masquerade as "set".
    """
    raw = os.environ.get("CHARTER_KEY_PASSPHRASE", "").strip()
    return raw.encode("utf-8") if raw else None


def save_private_key(private_key: Ed25519PrivateKey, path: Path) -> None:
    """Save an Ed25519 private key as PEM.

    If `CHARTER_KEY_PASSPHRASE` is set, the PEM is encrypted with
    `BestAvailableEncryption`. Otherwise it is written in plaintext and
    a WARN log is emitted.
    """
    passphrase = _key_passphrase()
    if passphrase is None:
        _log.warning(
            "writing unencrypted Ed25519 private key; do not use in production",
            extra={"path": str(path), "outcome": "plaintext"},
        )
        algorithm: serialization.KeySerializationEncryption = serialization.NoEncryption()
    else:
        algorithm = serialization.BestAvailableEncryption(passphrase)

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=algorithm,
    )
    path.write_bytes(pem)


def _pem_is_encrypted(pem: bytes) -> bool:
    """Detect whether a PEM block is an encrypted PKCS#8 key.

    Cheaper and clearer than try/except — we read the BEGIN header.
    PKCS#8 plaintext keys start with `-----BEGIN PRIVATE KEY-----`,
    PKCS#8 encrypted ones with `-----BEGIN ENCRYPTED PRIVATE KEY-----`.
    """
    return b"BEGIN ENCRYPTED PRIVATE KEY" in pem


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM file.

    If the file on disk is encrypted, requires `CHARTER_KEY_PASSPHRASE`
    to match. If the file is plaintext, loads it directly — the
    passphrase env (if set) is ignored and an INFO log records that the
    deployer probably wants to re-save the key under the new passphrase.

    Raises ValueError on any decryption failure or non-Ed25519 content.
    """
    pem = path.read_bytes()
    passphrase = _key_passphrase()

    key: object
    if _pem_is_encrypted(pem):
        if passphrase is None:
            raise ValueError(f"{path} is encrypted but CHARTER_KEY_PASSPHRASE is not set")
        key = serialization.load_pem_private_key(pem, password=passphrase)
    else:
        if passphrase is not None:
            _log.info(
                "loaded plaintext private key while a passphrase was configured "
                "(re-save under the new passphrase to opt into encryption)",
                extra={"path": str(path), "outcome": "plaintext_fallback"},
            )
        key = serialization.load_pem_private_key(pem, password=None)

    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def public_key_to_string(public_key: Ed25519PublicKey) -> str:
    """Encode a public key as `ed25519:<base64>` for use in provenance."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return f"ed25519:{base64.b64encode(raw).decode('ascii')}"


def public_key_from_string(s: str) -> Ed25519PublicKey:
    """Decode a `ed25519:<base64>` string back into a public-key object."""
    if not s.startswith("ed25519:"):
        raise ValueError(f"Expected 'ed25519:<base64>' prefix, got {s[:32]!r}")
    raw = base64.b64decode(s.removeprefix("ed25519:"))
    return Ed25519PublicKey.from_public_bytes(raw)


# ---------------------------------------------------------------------------
# JWK / kid helpers (v0.8 trust-model upgrade)
# ---------------------------------------------------------------------------


def kid_for_public_key(public_key_str: str) -> str:
    """Compute a stable JWKS `kid` for an Ed25519 public key.

    Format: first 16 lowercase-hex chars of `sha256(raw_public_key)`.
    Stable across processes and machines — same key → same `kid`.

    Args:
        public_key_str: `ed25519:<base64>` form (as it appears in
            `Charter.provenance.issuer_public_key`).

    Returns:
        16-char hex string suitable for a JWK `kid` field.
    """
    if not public_key_str.startswith("ed25519:"):
        raise ValueError(f"Expected 'ed25519:<base64>' prefix, got {public_key_str[:32]!r}")
    raw = base64.b64decode(public_key_str.removeprefix("ed25519:"))
    return hashlib.sha256(raw).hexdigest()[:16]


def public_key_to_jwk(public_key_str: str, *, kid: str | None = None) -> dict[str, str]:
    """Render an Ed25519 public key as an RFC 7517 JWK dict.

    Args:
        public_key_str: `ed25519:<base64>` form.
        kid: Optional override; otherwise derived via `kid_for_public_key`.

    Returns:
        A JWK dict with `kty="OKP"`, `crv="Ed25519"`, base64url-encoded
        `x` (raw public-key bytes, no padding), `use="sig"`, `alg="EdDSA"`,
        and the resolved `kid`.
    """
    if not public_key_str.startswith("ed25519:"):
        raise ValueError(f"Expected 'ed25519:<base64>' prefix, got {public_key_str[:32]!r}")
    raw = base64.b64decode(public_key_str.removeprefix("ed25519:"))
    x_b64url = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": kid if kid is not None else kid_for_public_key(public_key_str),
        "x": x_b64url,
        "use": "sig",
        "alg": "EdDSA",
    }


# ---------------------------------------------------------------------------
# Canonical serialization (for signing)
# ---------------------------------------------------------------------------


def _canonical_bytes(charter: Charter) -> bytes:
    """Serialize a Charter for signing.

    `issuer_signature` is cleared to break the self-reference, and
    `transparency_log_id` is cleared because the log entry can only be
    written AFTER the signature is final (chicken-and-egg). JSON is
    written with sorted keys and no whitespace, which is enough for v0;
    a formal canonical-JSON spec is out of scope (see future work).

    ADR-011 path 1 (privacy / redaction) adds one more exclusion rule:
    the matching Disclosure plaintexts NEVER enter the canonical bytes.
    The Charter only ever holds `disclosure_hash` commitments inside
    `Clause.private_fields`, so the signature commits to "this clause
    contained a value whose SHA-256 was X" but reveals nothing about
    X itself. Backward compatibility for pre-ADR-011 Charters is
    preserved by dropping `private_fields` from each clause when it is
    None — otherwise Pydantic would serialise `"private_fields": null`
    and break verification of every Charter signed before this field
    existed.
    """
    payload = charter.model_dump(mode="json")
    payload["provenance"]["issuer_signature"] = ""
    payload["provenance"]["transparency_log_id"] = None
    # Backward-compat: pre-ADR-011 Charters have no `private_fields`
    # key at all. We drop None occurrences so the canonical bytes are
    # byte-identical to what those Charters signed originally.
    for clause in payload.get("clauses", []):
        if clause.get("private_fields") is None:
            clause.pop("private_fields", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_charter(charter: Charter, private_key: Ed25519PrivateKey) -> Charter:
    """Sign a Charter in place and return it.

    The caller is responsible for setting `provenance.issuer_public_key` to
    match this private key before calling sign_charter — typically done at
    Charter construction time.

    Side effects:
        - `provenance.issuer_kid` is populated automatically from the
          embedded public key (v0.8+). The `kid` becomes part of the
          signed payload so verifiers can detect attempts to swap it.
        - `provenance.issuer_signature` is set to the final signature.
        - The signed Charter is appended to the v0.8 transparency log
          (`data/transparency.log`). Idempotent on `charter_id`, so
          re-signing (e.g. revoke / renew) does NOT create a new log
          entry — the original issuance entry stays the source of truth.
    """
    # Populate kid BEFORE building canonical bytes so it's covered by the
    # signature. If a caller already set it (e.g. testing with a fixed
    # value) we leave it alone.
    if charter.provenance.issuer_kid is None:
        charter.provenance.issuer_kid = kid_for_public_key(charter.provenance.issuer_public_key)

    payload = _canonical_bytes(charter)
    signature = private_key.sign(payload)
    encoded = f"ed25519:{base64.b64encode(signature).decode('ascii')}"
    charter.provenance.issuer_signature = encoded

    # Append to the transparency log. Lazy-import to avoid a circular import
    # with charter.storage / charter.schema during module init.
    from . import transparency

    entry = transparency.append(charter)
    # Record where the Charter landed in the log so calling agents can
    # jump directly to /transparency/proof/<charter_id> without scanning.
    # This field is OUTSIDE the canonical bytes — see `_canonical_bytes`.
    charter.provenance.transparency_log_id = entry.seq

    return charter


def verify_charter(charter: Charter) -> bool:
    """Verify a Charter's `issuer_signature` against its embedded public key.

    Returns True iff the signature is valid. Does NOT check expiry, revocation,
    or lifecycle status — those are policy decisions for the caller.
    """
    sig_str = charter.provenance.issuer_signature
    if not sig_str.startswith("ed25519:"):
        return False
    try:
        signature = base64.b64decode(sig_str.removeprefix("ed25519:"))
        public_key = public_key_from_string(charter.provenance.issuer_public_key)
        payload = _canonical_bytes(charter)
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# AdHocGrant signing (B2.5 — step-up protocol, ADR-013)
# ---------------------------------------------------------------------------


def _canonical_grant_bytes(grant: AdHocGrant) -> bytes:
    """Serialize an AdHocGrant for signing.

    Same canonical-bytes rule as Charter (ADR-003): sorted keys, no
    whitespace, `issuer_signature` cleared to break the self-reference.
    The grant does NOT have a `transparency_log_id` field — step-up
    grants are intentionally not transparency-logged in v0.9 (per
    ADR-013 future-work note).
    """
    payload = grant.model_dump(mode="json")
    payload["issuer_signature"] = ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_grant(grant: AdHocGrant, private_key: Ed25519PrivateKey) -> AdHocGrant:
    """Sign an AdHocGrant in place and return it.

    The caller is responsible for setting `issuer_kid` to the same value
    as the underlying Charter's `provenance.issuer_kid` before signing —
    a verifier matches the grant to a Charter's issuer key via this kid.

    No transparency log append (deliberate v0.9 scope: step-up grants
    are not transparency-logged; see ADR-013).
    """
    payload = _canonical_grant_bytes(grant)
    signature = private_key.sign(payload)
    grant.issuer_signature = f"ed25519:{base64.b64encode(signature).decode('ascii')}"
    return grant


def verify_grant_signature(grant: AdHocGrant, issuer_public_key_str: str) -> bool:
    """Verify a grant's signature against the issuer's public key.

    The issuer key must be supplied by the caller (typically fetched
    from the linked Charter's `provenance.issuer_public_key`). The
    grant itself does NOT embed its own issuer public key — the link
    to the Charter is the trust root.

    Returns True iff the signature is well-formed AND verifies. Does
    NOT check `expires_at` lifecycle (callers do that separately so
    they can produce different error types per failure mode).
    """
    sig_str = grant.issuer_signature
    if not sig_str.startswith("ed25519:"):
        return False
    try:
        signature = base64.b64decode(sig_str.removeprefix("ed25519:"))
        public_key = public_key_from_string(issuer_public_key_str)
        payload = _canonical_grant_bytes(grant)
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False
