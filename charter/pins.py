"""Key-fingerprint pinning for issuer Charters (v0.8).

Detects surprise key rotation. The first time a calling agent fetches a
Charter from a given issuer (principal), we record the fingerprint of the
verifying public key. Subsequent fetches verify the current key against
the pin. A mismatch raises `CharterPinMismatchError` and is treated as
`incompatible` by the protocol layer.

This is the layer that catches: "the host got compromised and is now
signing with the attacker's key" — even if the attacker also rotated the
JWKS, the local pin still flags it.

Storage: `data/pins.json` (override path with `CHARTER_PIN_FILE`).

    {
      "alice@acme.com": {
        "fingerprint":   "sha256:a1b2c3...",
        "first_seen":    "2026-05-18T12:00:00Z",
        "last_verified": "2026-05-18T15:42:00Z"
      }
    }

Writes are atomic (write-temp + rename) so a crash mid-write can't leave
a half-written pin file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ._logging import get_logger
from .storage import data_root

_log = get_logger("charter.pins")


@dataclass(frozen=True)
class Pin:
    """One pinned fingerprint record."""

    fingerprint: str
    first_seen: datetime
    last_verified: datetime


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def pin_file_path() -> Path:
    """Resolve the pin file path. Honors `CHARTER_PIN_FILE` override."""
    env = os.environ.get("CHARTER_PIN_FILE", "").strip()
    if env:
        return Path(env)
    return data_root() / "pins.json"


def fingerprint_of(public_key_str: str) -> str:
    """Compute the SHA-256 fingerprint of an `ed25519:<base64>` public key.

    Format: `sha256:<hex>`, full 64-hex-char digest. The full digest is
    used (not truncated like JWKS `kid`) so a pin is collision-resistant
    even when an attacker can pick which key they sign with.
    """
    if not public_key_str.startswith("ed25519:"):
        raise ValueError(f"Expected 'ed25519:<base64>' prefix, got {public_key_str[:32]!r}")
    raw = base64.b64decode(public_key_str.removeprefix("ed25519:"))
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _read_all() -> dict[str, dict[str, str]]:
    path = pin_file_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        _log.warning(
            "pin file unreadable; treating as empty",
            extra={"path": str(path), "error": str(e), "outcome": "read_error"},
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _write_all(table: dict[str, dict[str, str]]) -> None:
    """Atomic write: dump to `<file>.tmp`, then rename over the real file."""
    path = pin_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(table, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def get_pin(principal_id: str) -> Pin | None:
    """Return the pin for `principal_id`, or None if no pin has been recorded."""
    entry = _read_all().get(principal_id)
    if entry is None:
        return None
    try:
        return Pin(
            fingerprint=entry["fingerprint"],
            first_seen=datetime.fromisoformat(entry["first_seen"]),
            last_verified=datetime.fromisoformat(entry["last_verified"]),
        )
    except (KeyError, ValueError, TypeError) as e:
        _log.warning(
            "pin entry malformed; ignoring",
            extra={
                "principal_id": principal_id,
                "error": str(e),
                "outcome": "malformed",
            },
        )
        return None


def record_pin(principal_id: str, fingerprint: str) -> Pin:
    """Record a fresh pin (first-fetch TOFU). Both timestamps are now."""
    now = _now()
    table = _read_all()
    table[principal_id] = {
        "fingerprint": fingerprint,
        "first_seen": now.isoformat(),
        "last_verified": now.isoformat(),
    }
    _write_all(table)
    _log.info(
        "pin recorded",
        extra={
            "principal_id": principal_id,
            "fingerprint": fingerprint,
            "outcome": "pinned",
        },
    )
    return Pin(fingerprint=fingerprint, first_seen=now, last_verified=now)


def update_last_verified(principal_id: str) -> None:
    """Refresh `last_verified=now` for an existing pin. Idempotent on the fingerprint."""
    table = _read_all()
    entry = table.get(principal_id)
    if entry is None:
        return
    entry["last_verified"] = _now().isoformat()
    _write_all(table)


def reset_pin(principal_id: str) -> bool:
    """Drop the pin. Returns True iff a pin was actually removed."""
    table = _read_all()
    if principal_id not in table:
        return False
    del table[principal_id]
    _write_all(table)
    _log.info(
        "pin reset",
        extra={"principal_id": principal_id, "outcome": "reset"},
    )
    return True


def list_pins() -> dict[str, Pin]:
    """Return all current pins keyed by principal_id."""
    raw = _read_all()
    out: dict[str, Pin] = {}
    for principal_id, entry in raw.items():
        try:
            out[principal_id] = Pin(
                fingerprint=entry["fingerprint"],
                first_seen=datetime.fromisoformat(entry["first_seen"]),
                last_verified=datetime.fromisoformat(entry["last_verified"]),
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out
