"""Charter Discovery — resolve `(principal_id, agent_id)` to a `charter_url`.

The protocol defines two discovery paths:

  1. **SaaS multi-tenant.** Every Charter lives at
     `{CHARTER_URL_BASE}/{principal_id}/{agent_id}`. If a caller already
     knows the base URL and the binding, no lookup is needed; they
     compose the URL directly.

  2. **Local + index file.** When Charters live on the same machine as
     the calling agent (development, CI, agent harnesses that ship
     pre-baked Charters), the `data/charters/index.json` file maps each
     known binding to its URL. `resolve_charter_url` consults the index
     first, falls back to the SaaS-composition rule second.

`storage.save_charter` keeps the index in sync automatically: every
issue / renew writes the new (binding -> URL) entry; every revoke
updates the existing one.

The MCP server's `/api/lookup` HTTP endpoint is the network-callable
equivalent of this helper and reads the same index file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .constants import DEFAULT_URL_BASE
from .errors import CharterNotFoundError
from .schema import Charter
from .storage import charters_dir


def _index_path() -> Path:
    return charters_dir() / "index.json"


def _read_index() -> dict[str, dict[str, str]]:
    """Read the index file. Returns `{principal_id: {agent_id: url}}`.

    Missing or unreadable index → empty dict (silent recover; index is a
    cache, not authoritative state).
    """
    path = _index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    # Validate shape minimally — guards against partial/corrupt writes.
    cleaned: dict[str, dict[str, str]] = {}
    for principal, agents in data.items():
        if isinstance(agents, dict):
            cleaned[principal] = {a: u for a, u in agents.items() if isinstance(u, str)}
    return cleaned


def _write_index(data: dict[str, dict[str, str]]) -> None:
    """Atomically rewrite the index file.

    Writes to a sibling temp file then renames so a crash mid-write
    doesn't leave a partially-written index.
    """
    path = _index_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _default_url_for(principal_id: str, agent_id: str, base: str | None = None) -> str:
    """Compose the SaaS-style URL for a binding."""
    effective_base = (base or os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE)).rstrip("/")
    return f"{effective_base}/{principal_id}/{agent_id}"


def update_index(charter: Charter, base: str | None = None) -> None:
    """Record a Charter's binding -> URL mapping in the local index.

    Called from `storage.save_charter` after each write so the index
    stays in sync with what's on disk. Safe to call with the same
    Charter multiple times — last write wins.
    """
    data = _read_index()
    p = charter.binding.principal_id
    a = charter.binding.agent_id
    data.setdefault(p, {})[a] = _default_url_for(p, a, base=base)
    _write_index(data)


def resolve_charter_url(
    principal_id: str,
    agent_id: str,
    *,
    base: str | None = None,
    strict: bool = False,
) -> str:
    """Resolve a `(principal_id, agent_id)` binding to a `charter_url`.

    Lookup order:

      1. Local `data/charters/index.json` — exact (principal, agent)
         match. This is the source of truth when Charters live on the
         same machine as the calling agent.
      2. `{base}/{principal_id}/{agent_id}` fallback — composes the
         canonical SaaS URL. `base` defaults to the
         `CHARTER_URL_BASE` env var, then to the protocol default.

    Args:
        principal_id:  Binding's principal.
        agent_id:      Binding's agent.
        base:          Explicit base URL override; useful for tests and
                       for callers that already know their host.
        strict:        If True, raise `CharterNotFoundError` instead of
                       falling back to the SaaS composition when the
                       binding is unknown locally.

    Returns:
        The canonical `charter_url` string for the binding.

    Raises:
        `CharterNotFoundError` if `strict=True` and the binding is not
        in the local index.
    """
    index = _read_index()
    if principal_id in index and agent_id in index[principal_id]:
        return index[principal_id][agent_id]

    if strict:
        raise CharterNotFoundError(
            f"No local index entry for binding {principal_id} x {agent_id}; "
            f"call resolve_charter_url(..., strict=False) to fall back to "
            f"{_default_url_for(principal_id, agent_id, base=base)}"
        )

    return _default_url_for(principal_id, agent_id, base=base)
