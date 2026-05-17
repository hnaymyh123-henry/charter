"""Local file I/O for Charters and issuer keys.

v0 stores everything under `./data/`:

    data/
      charters/
        <principal_id>__<agent_id>.json      <- one Charter per binding
      keys/
        <principal_id>.pem                   <- one Ed25519 private key per issuer

A `data/charters/index.json` directory file maps
`(principal_id, agent_id) -> filename` for `resolve_charter_url` lookups
(v0+ extension; demo can skip the SDK helper and just compose URLs directly).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .constants import DEFAULT_DATA_DIR
from .schema import Charter
from .signing import generate_keypair, load_private_key, save_private_key


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def data_root() -> Path:
    return Path(os.environ.get("CHARTER_DATA_DIR", DEFAULT_DATA_DIR))


def charters_dir() -> Path:
    d = data_root() / "charters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def keys_dir() -> Path:
    d = data_root() / "keys"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(s: str) -> str:
    """Map a principal_id or agent_id to a filesystem-safe segment."""
    return s.replace("/", "_").replace(":", "_").replace("@", "_at_")


def charter_path(principal_id: str, agent_id: str) -> Path:
    return charters_dir() / f"{_safe(principal_id)}__{_safe(agent_id)}.json"


def key_path(principal_id: str) -> Path:
    return keys_dir() / f"{_safe(principal_id)}.pem"


# ---------------------------------------------------------------------------
# Charter I/O
# ---------------------------------------------------------------------------

def save_charter(charter: Charter) -> Path:
    """Write a Charter JSON to disk. Returns the path written."""
    path = charter_path(charter.binding.principal_id, charter.binding.agent_id)
    path.write_text(charter.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_charter(principal_id: str, agent_id: str) -> Optional[Charter]:
    """Load a Charter by binding, or None if not found."""
    path = charter_path(principal_id, agent_id)
    if not path.exists():
        return None
    return Charter.model_validate_json(path.read_text(encoding="utf-8"))


def load_charter_by_path(path: Path) -> Charter:
    """Load a Charter from an explicit path (used by FastAPI)."""
    return Charter.model_validate_json(path.read_text(encoding="utf-8"))


def list_charters() -> list[Charter]:
    """List all Charters under the local data directory."""
    out: list[Charter] = []
    for p in sorted(charters_dir().glob("*.json")):
        if p.name == "index.json":
            continue
        try:
            out.append(load_charter_by_path(p))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Issuer key I/O (with auto-create-on-demand for demo convenience)
# ---------------------------------------------------------------------------

def ensure_issuer_key(principal_id: str) -> Ed25519PrivateKey:
    """Return the issuer's private key, creating one if absent.

    Demo convenience: in production the issuer key would be stored in a
    secrets manager or HSM, not on disk. v0 trusts the local filesystem.
    """
    path = key_path(principal_id)
    if path.exists():
        return load_private_key(path)
    private, _ = generate_keypair()
    save_private_key(private, path)
    return private
