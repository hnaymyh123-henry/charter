"""Local file I/O for Charters and issuer keys.

Layout under `data/`:

    data/
      charters/
        <principal_id>__<agent_id>.json      live Charter for the binding
        archive/
          <safe_charter_id>.json             superseded/revoked predecessors
      keys/
        <principal_id>.pem                   Ed25519 private key per issuer
      disclosures/
        <safe_charter_id>/
          <disclosure_id>.json               ADR-011 path 1 plaintexts

The live Charter is the one served by the FastAPI host at
`/{principal}/{agent}`. The archive directory exists so that a renewed or
revoked Charter's predecessor is still recoverable by `charter_id` —
useful for audit trails and for resolving a `replaces` / `replaced_by`
chain when a calling agent only knows the old `charter_id`.

`disclosures/` was added in ADR-011 path 1. Each `Disclosure` is stored
in its own file so the bearer-token-gated `GET /disclosures/...`
endpoint can serve a single record without ever loading the others.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .constants import DEFAULT_DATA_DIR
from .privacy import Disclosure
from .schema import Charter
from .signing import generate_keypair, load_private_key, save_private_key

# Allowlist of characters that may appear unchanged in a filesystem segment
# derived from untrusted input. Everything else is replaced by `_`.
#
# Why an allowlist (vs blacklist replace of `/` `:` `@`)?
# A blacklist forgets characters: Windows path separator `\`, parent-dir
# traversal `..`, null bytes, leading/trailing whitespace, percent-encoded
# variants that FastAPI decodes before reaching us. An allowlist makes
# "what is allowed" the explicit contract — a new attack vector that uses
# a character not in this set is automatically defused, not retroactively
# patched.
_SAFE_ALLOWLIST = re.compile(r"[^A-Za-z0-9._-]")

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
    """Map untrusted input (principal_id, agent_id, charter_id,
    disclosure_id, ...) to a single, safe filesystem segment.

    Hardened against path-traversal: anything outside the allowlist
    ``[A-Za-z0-9._-]`` is replaced with ``_``. In particular:

      - ``/`` and ``\\``  -> ``_``  (POSIX and Windows separators)
      - ``..``           ->  ``__`` is not produced because `.` IS allowed,
        so the literal segment ``..`` is collapsed to ``__`` only when
        adjacent to another disallowed char. The boundary check in
        :func:`disclosure_path` is the load-bearing guarantee, not this.
      - ``:`` and ``@``  -> ``_``  (kept for back-compat with the older
        blacklist behaviour that produced ``_at_`` for ``@``)
      - null byte ``\\x00``, control chars, unicode RTL overrides,
        percent-encoded path bytes already decoded by FastAPI -> ``_``
      - leading/trailing whitespace stripped before sanitisation

    The literal segments ``.`` and ``..`` are explicitly mapped to ``_``
    and ``__`` so the resulting segment can never act as "this dir" or
    "parent dir" when joined into a Path.

    Empty / whitespace-only input is rejected with ``ValueError`` —
    silently mapping ``""`` to a falsy filesystem segment would let an
    attacker control which directory is opened by the caller of
    :func:`disclosure_path`.
    """
    stripped = s.strip()
    if not stripped:
        raise ValueError("filesystem segment cannot be empty")
    safe = _SAFE_ALLOWLIST.sub("_", stripped)
    # After substitution, defang the two segments that the filesystem
    # treats specially. Mapping both to underscore-padded versions keeps
    # the segment non-empty and not equal to "." or "..".
    if safe == ".":
        return "_"
    if safe == "..":
        return "__"
    return safe


def charter_path(principal_id: str, agent_id: str) -> Path:
    return charters_dir() / f"{_safe(principal_id)}__{_safe(agent_id)}.json"


def archive_dir() -> Path:
    d = charters_dir() / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_path(charter_id: str) -> Path:
    return archive_dir() / f"{_safe(charter_id)}.json"


def key_path(principal_id: str) -> Path:
    return keys_dir() / f"{_safe(principal_id)}.pem"


def disclosures_root() -> Path:
    """`data/disclosures/` — root for ADR-011 path 1 plaintexts."""
    d = data_root() / "disclosures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def disclosures_dir(charter_id: str) -> Path:
    """`data/disclosures/<safe_charter_id>/` for one Charter's disclosures."""
    d = disclosures_root() / _safe(charter_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def disclosure_path(charter_id: str, disclosure_id: str) -> Path:
    """Per-disclosure JSON file path.

    Both ``charter_id`` and ``disclosure_id`` are treated as untrusted
    HTTP path parameters (the FastAPI route exposes them directly).
    Defense in depth:

      1. ``_safe()`` strips every char outside the allowlist (including
         ``..``, ``/``, ``\\``, null bytes, control chars, percent-decoded
         path separators FastAPI hands us as raw bytes).
      2. After joining, the result is resolved and verified to live
         under ``disclosures_root()``. If a future change to ``_safe``
         or the join logic ever lets a traversal slip through, this
         check raises ``ValueError`` rather than returning a path that
         escapes the disclosures directory.

    Caller (``load_disclosure`` / server endpoint) catches the
    ``ValueError`` and translates to the same 404 every other failure
    mode returns, so the attacker cannot use response shape to
    distinguish "valid id, missing file" from "traversal blocked".
    """
    candidate = disclosures_dir(charter_id) / f"{_safe(disclosure_id)}.json"
    # Final boundary check — even if `_safe` ever regresses, the
    # resolved path must live under disclosures_root(). `resolve()`
    # collapses any residual `..` segments; `is_relative_to` is the
    # 3.9+ replacement for the older try/except-on-relative_to idiom.
    root = disclosures_root().resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(
            f"disclosure path escapes disclosures_root: "
            f"charter_id={charter_id!r}, disclosure_id={disclosure_id!r}"
        )
    return candidate


# ---------------------------------------------------------------------------
# Charter I/O
# ---------------------------------------------------------------------------


def save_charter(charter: Charter) -> Path:
    """Write a Charter JSON to disk at the canonical binding path.

    Returns the path written. This is the "live" Charter for the
    binding. Predecessors with `status in {"superseded", "revoked"}` are
    written to the archive via `archive_charter` instead, so the live
    binding path always holds the currently-authoritative Charter.

    Side effect: updates `data/charters/index.json` so
    `discovery.resolve_charter_url` can resolve this binding locally.
    """
    path = charter_path(charter.binding.principal_id, charter.binding.agent_id)
    path.write_text(charter.model_dump_json(indent=2), encoding="utf-8")

    # Keep the discovery index in sync. Imported lazily so this module
    # has no circular dependency with charter.discovery (which imports
    # charters_dir from this module).
    from .discovery import update_index

    update_index(charter)

    return path


def archive_charter(charter: Charter) -> Path:
    """Write a superseded or revoked Charter to the archive directory.

    Used by `charter renew` so the predecessor remains queryable by
    `charter_id` after the live binding path is overwritten by the
    successor. Returns the archived path.
    """
    path = archive_path(charter.charter_id)
    path.write_text(charter.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_charter(principal_id: str, agent_id: str) -> Charter | None:
    """Load the live Charter for a binding, or None if not found."""
    path = charter_path(principal_id, agent_id)
    if not path.exists():
        return None
    return Charter.model_validate_json(path.read_text(encoding="utf-8"))


def load_archived_charter(charter_id: str) -> Charter | None:
    """Load a previously archived (superseded/revoked) Charter by id."""
    path = archive_path(charter_id)
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


def list_known_issuer_keys() -> list[tuple[str, str]]:
    """Walk all live Charters and return distinct (principal_id, public_key) pairs.

    Used to assemble the JWKS at `/.well-known/jwks.json`. Dedupes on
    `(principal_id, public_key_string)` — a single issuer with one key
    becomes one entry; an issuer that has rotated produces multiple
    entries (the JWKS exposes both so old Charters still verify).

    Output is sorted for deterministic ordering across calls.
    """
    seen: set[tuple[str, str]] = set()
    for charter in list_charters():
        seen.add((charter.issuer.id, charter.provenance.issuer_public_key))
    return sorted(seen)


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


# ---------------------------------------------------------------------------
# Disclosure I/O (ADR-011 path 1)
# ---------------------------------------------------------------------------


def save_disclosure(charter_id: str, disclosure: Disclosure) -> Path:
    """Persist one Disclosure under `data/disclosures/<charter>/<id>.json`.

    Plaintext on disk is intentional — the file system is the trust
    boundary for ADR-011 path 1. Operators are expected to mount this
    directory only on hosts that already hold the issuer's private key,
    so anyone able to read the disclosure could already sign new
    Charters anyway.
    """
    path = disclosure_path(charter_id, disclosure.disclosure_id)
    path.write_text(disclosure.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_disclosure(charter_id: str, disclosure_id: str) -> Disclosure | None:
    """Load one Disclosure record, or None if absent.

    The bearer-token-gated server endpoint at
    `GET /disclosures/{charter_id}/{disclosure_id}` is the only
    runtime caller. Returns None for all of:

      - no such file on disk
      - file exists but is corrupt / not valid JSON
      - input is rejected by ``_safe`` (empty segment after sanitisation)

    The endpoint translates all of these into the same 404 so an
    attacker cannot use response shape to distinguish between them.
    """
    try:
        path = disclosure_path(charter_id, disclosure_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return Disclosure.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_disclosures(charter_id: str) -> list[Disclosure]:
    """Load every Disclosure stored for one Charter, sorted by file name.

    Used by issuer-side tooling that wants to inspect or re-publish
    every disclosure at once (CLI dumps, audit). The runtime server
    endpoint uses `load_disclosure` for single records.
    """
    out: list[Disclosure] = []
    root = disclosures_root() / _safe(charter_id)
    if not root.exists():
        return out
    for p in sorted(root.glob("*.json")):
        try:
            out.append(Disclosure.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out
