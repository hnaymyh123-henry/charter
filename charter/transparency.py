"""Append-only transparency log for signed Charters (v0.8).

Every signed Charter gets appended here so third-party auditors can later
walk the log and detect retroactive tampering — the classic "did this
issuer sign a Charter for me that I never approved?" question.

This is the third leg of the v0.8 trust model:
  - JWKS (#20) answers "which key signed this Charter?"
  - Pinning (#21) answers "is this the key I saw before?"
  - Transparency log (#22) answers "what else has this issuer signed?"

## Format

`data/transparency.log` is JSON Lines, one entry per line:

    {
      "seq": 1,
      "charter_id": "charter:alice@acme.com:research_agent_v1:2026-05-18",
      "binding": {"principal_id": "...", "agent_id": "..."},
      "issuer_kid": "a1b2c3d4e5f67890",
      "issuer_signature": "ed25519:...",
      "appended_at": "2026-05-18T15:42:00Z",
      "prev_hash": "sha256:0000...",
      "entry_hash": "sha256:..."
    }

`entry_hash` is computed over the canonical JSON of the entry with
`entry_hash` itself excluded. The first entry's `prev_hash` is the
genesis value (all zeros).

## Crash safety

Append rebuilds the whole file via temp + `os.replace`. Slow for huge logs
but Charter issuance is rare and an atomic swap means a crash can never
leave a half-written entry. Aggregating into a real append-with-fsync
implementation is on the v0.9+ backlog.

## What is and isn't in the log

The log stores the Charter's IDENTIFIERS and SIGNATURE — enough to prove
issuance happened. The Charter BODY (clauses, summary, principal role
text) is NOT in the log; the Charter file itself stays in
`data/charters/`. This keeps the log small, lets audit clients fetch
just enough to validate the chain, and avoids re-publishing the
sometimes-sensitive clause text every time the log is shared.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ._logging import get_logger
from .observability import charter_span_cm, set_span_attrs
from .schema import Charter
from .storage import data_root

_log = get_logger("charter.transparency")

GENESIS_PREV_HASH = "sha256:" + "0" * 64


@dataclass(frozen=True)
class TransparencyEntry:
    """One line in the transparency log."""

    seq: int
    charter_id: str
    binding: dict[str, str]
    issuer_kid: str | None
    issuer_signature: str
    appended_at: datetime
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "charter_id": self.charter_id,
            "binding": self.binding,
            "issuer_kid": self.issuer_kid,
            "issuer_signature": self.issuer_signature,
            "appended_at": self.appended_at.isoformat(),
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> TransparencyEntry:
        binding_raw = raw["binding"]
        if not isinstance(binding_raw, dict):
            raise ValueError("entry 'binding' must be a dict")
        seq_raw = raw["seq"]
        if not isinstance(seq_raw, int):
            raise ValueError("entry 'seq' must be an int")
        return cls(
            seq=seq_raw,
            charter_id=str(raw["charter_id"]),
            binding={str(k): str(v) for k, v in binding_raw.items()},
            issuer_kid=(str(raw["issuer_kid"]) if raw.get("issuer_kid") is not None else None),
            issuer_signature=str(raw["issuer_signature"]),
            appended_at=datetime.fromisoformat(str(raw["appended_at"])),
            prev_hash=str(raw["prev_hash"]),
            entry_hash=str(raw["entry_hash"]),
        )


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking the log. `ok=True` iff every prev_hash matches
    the previous entry's entry_hash and every entry's entry_hash matches
    its content."""

    ok: bool
    entries: int
    head_hash: str
    broken_at_seq: int | None
    reason: str | None


def log_file_path() -> Path:
    """Resolve the log path. Honors `CHARTER_TRANSPARENCY_LOG` override."""
    env = os.environ.get("CHARTER_TRANSPARENCY_LOG", "").strip()
    if env:
        return Path(env)
    return data_root() / "transparency.log"


def _canonical_json(payload: dict[str, object]) -> bytes:
    """Sorted-keys, compact JSON. Same convention as Charter signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _hash_entry_fields(payload_without_entry_hash: dict[str, object]) -> str:
    """Compute `sha256:<hex>` over the entry's canonical JSON sans entry_hash."""
    digest = hashlib.sha256(_canonical_json(payload_without_entry_hash)).hexdigest()
    return f"sha256:{digest}"


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_log() -> Iterator[TransparencyEntry]:
    """Yield every entry in the log in sequence order. Empty log -> no yields."""
    for line in _read_lines(log_file_path()):
        try:
            raw = json.loads(line)
        except ValueError as e:
            _log.warning(
                "transparency log line unparseable; skipping",
                extra={"error": str(e), "outcome": "parse_error"},
            )
            continue
        yield TransparencyEntry.from_dict(raw)


def head() -> TransparencyEntry | None:
    """Return the last entry (highest seq) or None if the log is empty."""
    last: TransparencyEntry | None = None
    for entry in read_log():
        last = entry
    return last


def get_entry(charter_id: str) -> TransparencyEntry | None:
    """Return the entry for `charter_id`, or None if not present."""
    for entry in read_log():
        if entry.charter_id == charter_id:
            return entry
    return None


def append(charter: Charter) -> TransparencyEntry:
    """Append a Charter's metadata to the log. Atomic.

    Stores `charter_id`, `binding`, `issuer_kid`, `issuer_signature`, and
    the chain hashes — NOT the Charter body. The function is idempotent
    on `charter_id`: re-appending the same Charter is a no-op and returns
    the existing entry, so retries after a crash are safe.

    Emits one `charter.transparency_append` OTel span per call with
    `charter.id` / `charter.seq` / `charter.verdict` attributes when
    OTel is installed.
    """
    with charter_span_cm(
        "charter.transparency_append",
        {"charter.id": charter.charter_id},
    ) as span:
        # Pre-check duplicate so the verdict is accurate. If the entry
        # already exists, _append_impl returns it as-is (idempotent).
        was_duplicate = get_entry(charter.charter_id) is not None
        entry = _append_impl(charter)
        set_span_attrs(
            span,
            {
                "charter.seq": entry.seq,
                "charter.verdict": "duplicate" if was_duplicate else "appended",
            },
        )
        return entry


def _append_impl(charter: Charter) -> TransparencyEntry:
    """Inner implementation. Span wrapper above captures the seq + verdict
    cleanly without indenting the whole body. Semantics unchanged."""
    existing = get_entry(charter.charter_id)
    if existing is not None:
        _log.info(
            "transparency append skipped (already logged)",
            extra={
                "charter_id": charter.charter_id,
                "seq": existing.seq,
                "outcome": "duplicate",
            },
        )
        return existing

    prev = head()
    next_seq = (prev.seq + 1) if prev is not None else 1
    prev_hash = prev.entry_hash if prev is not None else GENESIS_PREV_HASH

    appended_at = datetime.now(UTC).replace(microsecond=0)
    payload: dict[str, object] = {
        "seq": next_seq,
        "charter_id": charter.charter_id,
        "binding": {
            "principal_id": charter.binding.principal_id,
            "agent_id": charter.binding.agent_id,
        },
        "issuer_kid": charter.provenance.issuer_kid,
        "issuer_signature": charter.provenance.issuer_signature,
        "appended_at": appended_at.isoformat(),
        "prev_hash": prev_hash,
    }
    entry_hash = _hash_entry_fields(payload)
    payload["entry_hash"] = entry_hash

    _atomic_append_line(json.dumps(payload, ensure_ascii=False))

    _log.info(
        "transparency append ok",
        extra={
            "charter_id": charter.charter_id,
            "seq": next_seq,
            "entry_hash": entry_hash,
            "outcome": "appended",
        },
    )
    return TransparencyEntry.from_dict(payload)


def _atomic_append_line(line: str) -> None:
    """Append one line via temp + `os.replace`. Slow but crash-safe."""
    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_lines(path)
    existing.append(line)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def verify_chain() -> ChainVerification:
    """Walk the log and verify every prev_hash + entry_hash.

    Returns a `ChainVerification` describing the result. The chain is
    intact iff every entry's `prev_hash` equals the previous entry's
    `entry_hash` AND every entry's `entry_hash` matches a fresh hash of
    its own fields. The audit primitive for `charter audit verify`.
    """
    entries: list[TransparencyEntry] = list(read_log())
    if not entries:
        return ChainVerification(
            ok=True,
            entries=0,
            head_hash=GENESIS_PREV_HASH,
            broken_at_seq=None,
            reason=None,
        )

    expected_prev = GENESIS_PREV_HASH
    for entry in entries:
        if entry.prev_hash != expected_prev:
            return ChainVerification(
                ok=False,
                entries=len(entries),
                head_hash=entries[-1].entry_hash,
                broken_at_seq=entry.seq,
                reason=(
                    f"prev_hash mismatch at seq={entry.seq}: "
                    f"expected {expected_prev}, found {entry.prev_hash}"
                ),
            )
        # Recompute entry_hash on the fly to catch in-place tampering of
        # any non-prev_hash field.
        recomputed = _hash_entry_fields(
            {k: v for k, v in entry.to_dict().items() if k != "entry_hash"}
        )
        if recomputed != entry.entry_hash:
            return ChainVerification(
                ok=False,
                entries=len(entries),
                head_hash=entries[-1].entry_hash,
                broken_at_seq=entry.seq,
                reason=(
                    f"entry_hash mismatch at seq={entry.seq}: "
                    f"recomputed {recomputed}, found {entry.entry_hash}"
                ),
            )
        expected_prev = entry.entry_hash

    return ChainVerification(
        ok=True,
        entries=len(entries),
        head_hash=entries[-1].entry_hash,
        broken_at_seq=None,
        reason=None,
    )
