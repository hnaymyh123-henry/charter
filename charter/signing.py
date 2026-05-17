"""Ed25519 keygen / sign / verify for Self-Attesting Charters.

A Charter is signed by the issuer's Ed25519 private key. The corresponding
public key is embedded inline at `provenance.issuer_public_key`, so anyone
fetching the Charter via HTTPS can verify the signature using only the JSON
itself.

Signing covers a canonical serialization of the Charter with the
`provenance.issuer_signature` field cleared. This avoids the chicken-and-egg
of signing a payload that contains its own signature.

Trust root: HTTPS to the charter_url host (TOFU on first fetch). See
§P2-9 for the demo-centric tradeoffs.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .schema import Charter


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Create a fresh Ed25519 keypair for an issuer."""
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


def save_private_key(private_key: Ed25519PrivateKey, path: Path) -> None:
    """Save an Ed25519 private key as PEM. The directory must already exist."""
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM file."""
    pem = path.read_bytes()
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
# Canonical serialization (for signing)
# ---------------------------------------------------------------------------

def _canonical_bytes(charter: Charter) -> bytes:
    """Serialize a Charter for signing.

    The signature field is cleared first to break the self-reference. JSON is
    written with sorted keys and no whitespace, which is enough for v0; a
    formal canonical-JSON spec is out of scope (see future work).
    """
    payload = charter.model_dump(mode="json")
    payload["provenance"]["issuer_signature"] = ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------

def sign_charter(charter: Charter, private_key: Ed25519PrivateKey) -> Charter:
    """Sign a Charter in place and return it.

    The caller is responsible for setting `provenance.issuer_public_key` to
    match this private key before calling sign_charter — typically done at
    Charter construction time.
    """
    payload = _canonical_bytes(charter)
    signature = private_key.sign(payload)
    encoded = f"ed25519:{base64.b64encode(signature).decode('ascii')}"
    charter.provenance.issuer_signature = encoded
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
