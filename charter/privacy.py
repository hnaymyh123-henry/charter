"""Per-span redaction with SD-JWT-style selective disclosure (ADR-011 path 1).

The Charter publishes clause text in the clear, but a clause may carry
named entities (customer names, case numbers, internal project codes)
that the principal does not want exposed to every fetcher. Path 1 of
the privacy layer replaces each sensitive span with a
``[REDACTED:<hash-prefix>]`` placeholder and commits to the SHA-256 of
``salt || plaintext`` in `Clause.private_fields`. The matching
``Disclosure`` record holds the plaintext + salt and is served from a
bearer-token-protected endpoint (or never released at all).

Why per-span instead of "hide the whole clause":

  - The calling agent's grader LLM still needs to judge whether the
    clause is hit by the intended task. Hiding the clause entirely
    makes it indistinguishable from a clause that does not exist —
    which silently loosens the verdict, the opposite of the goal.
  - Keeping clause `type` and structure public preserves the
    deterministic `TYPE_TO_DECISION` mapping (ADR-004) and the
    aggregation precedence (ADR-005).

Why SHA-256(salt || value) and not bare SHA-256(value):

  - Customer names and case numbers come from a small enumerable
    universe. Without a salt, an attacker would dictionary-attack the
    hash trivially. The 16-byte salt is per-disclosure so two clauses
    that redact the same value get different placeholders + different
    commitments — no cross-clause correlation either.

The four public helpers below are the entire surface:

  - ``redact_clause`` — issuer-side. Build the Charter clause + the
    list of Disclosure records to persist alongside it.
  - ``verify_disclosure`` — caller-side. Confirm that a fetched
    Disclosure matches the hash committed in `private_fields`.
  - ``match_redacted`` — caller-side. "Is this candidate value the
    plaintext behind any redacted span in this clause?" — answered
    locally, without ever fetching the Disclosure.
  - ``Disclosure`` — Pydantic model written to / read from the
    `data/disclosures/` directory by `charter.storage`.
"""

from __future__ import annotations

import hashlib
import secrets

from pydantic import BaseModel, ConfigDict, Field

from .schema import PrivateFieldRef

# 16 bytes ≈ 128 bits of unguessable salt — same magnitude SD-JWT uses,
# and far more than the dictionary attack on enumerable names requires
# to be infeasible.
SALT_BYTES = 16

# Placeholder length kept short on purpose: it has to be readable inline
# in clause text. The 8-hex prefix gives 32 bits of disambiguation
# within a clause, plenty for human inspection ("is this the same
# placeholder I saw above?") without padding the prompt with noise.
HASH_PREFIX_HEX_CHARS = 8


# ---------------------------------------------------------------------------
# Disclosure model
# ---------------------------------------------------------------------------


class Disclosure(BaseModel):
    """One revealed plaintext + the salt and hash that bind it to a
    `PrivateFieldRef` inside a Charter clause.

    Stored under `data/disclosures/<safe_charter_id>/<disclosure_id>.json`
    and served (with bearer-token auth) from
    ``GET /disclosures/{charter_id}/{disclosure_id}``. A caller that
    successfully fetches a Disclosure SHOULD call `verify_disclosure`
    before trusting `span_value`.
    """

    model_config = ConfigDict(extra="forbid")

    disclosure_id: str
    span_value: str
    salt_hex: str  # hex-encoded raw salt bytes (no `0x` prefix)
    disclosure_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Hash + placeholder construction
# ---------------------------------------------------------------------------


def _hash_value(salt: bytes, value: str) -> str:
    """Compute ``sha256:<hex>`` commitment for ``salt || value``.

    UTF-8 encoding for the value side is fixed — every implementation
    needs the same byte sequence to recompute the hash, and UTF-8 is
    the only sane choice for clause-text content.
    """
    digest = hashlib.sha256(salt + value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _placeholder_for(disclosure_hash: str) -> str:
    """Render the in-text placeholder, e.g. ``[REDACTED:1a2b3c4d]``.

    The first 8 hex chars of the hash drive the suffix. Two redactions
    of the same value with different salts produce different prefixes,
    so the placeholder doubles as a visual disambiguator without
    leaking the plaintext.
    """
    if not disclosure_hash.startswith("sha256:"):
        raise ValueError("disclosure_hash must start with 'sha256:'")
    return f"[REDACTED:{disclosure_hash[len('sha256:') : len('sha256:') + HASH_PREFIX_HEX_CHARS]}]"


def _make_disclosure_id(salt: bytes, value: str) -> str:
    """Derive a deterministic disclosure_id from the same hash input.

    Using the hash prefix as the id keeps disclosure files and
    `[REDACTED:<...>]` placeholders aligned 1-to-1, which makes manual
    debugging tractable. Collisions across a single Charter are
    extremely unlikely (different salts ensure different prefixes
    even when the plaintext repeats) but `redact_clause` ALSO
    de-duplicates against the existing id list defensively.
    """
    digest = hashlib.sha256(salt + value.encode("utf-8")).hexdigest()
    return digest[:HASH_PREFIX_HEX_CHARS]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def redact_clause(
    clause_text: str,
    private_spans: list[tuple[int, int]],
    salt: bytes | None = None,
) -> tuple[str, list[PrivateFieldRef], list[Disclosure]]:
    """Build the redacted clause text + the public + private artefacts.

    Args:
        clause_text: the original (sensitive) clause text.
        private_spans: ``[(start, end), ...]`` half-open intervals into
            ``clause_text``. MUST be non-overlapping; ``redact_clause``
            sorts them defensively but raises ValueError on overlap.
        salt: only here for deterministic tests. When None (the
            production path) a fresh `SALT_BYTES`-byte salt is drawn
            per span via ``secrets.token_bytes``. When provided, ALL
            spans share the salt — useful for golden-file unit tests,
            but never for real Charters.

    Returns:
        ``(redacted_text, private_fields, disclosures)`` where the
        coordinates inside each ``PrivateFieldRef`` point INTO the
        returned ``redacted_text`` (NOT the original), so callers can
        locate the placeholder without reconstructing the source.
    """
    if not private_spans:
        return clause_text, [], []

    # Sort by start ascending so we can rebuild the text in one pass.
    spans = sorted(private_spans, key=lambda s: s[0])
    for i in range(len(spans) - 1):
        if spans[i][1] > spans[i + 1][0]:
            raise ValueError(
                f"overlapping private spans: {spans[i]} and {spans[i + 1]}; "
                "ADR-011 path 1 requires non-overlapping redactions"
            )

    out_parts: list[str] = []
    private_fields: list[PrivateFieldRef] = []
    disclosures: list[Disclosure] = []
    seen_ids: set[str] = set()
    cursor = 0
    for start, end in spans:
        if start < 0 or end > len(clause_text) or start >= end:
            raise ValueError(
                f"invalid span ({start}, {end}) for clause of length {len(clause_text)}"
            )
        value = clause_text[start:end]
        span_salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
        disclosure_hash = _hash_value(span_salt, value)
        placeholder = _placeholder_for(disclosure_hash)

        # Build redacted-text slice up to here, then drop the placeholder.
        out_parts.append(clause_text[cursor:start])
        # Coordinates against the OUTPUT string. Recompute as we go so
        # subsequent placeholders line up correctly.
        redacted_so_far_len = sum(len(p) for p in out_parts)
        out_parts.append(placeholder)
        cursor = end

        disc_id = _make_disclosure_id(span_salt, value)
        # Defensive: if two spans happen to share the same hash prefix
        # (different salts, same id is astronomically unlikely; same
        # plaintext + same salt could produce one if a caller forces
        # `salt`), append a counter so the disclosure_id stays unique
        # per Charter.
        suffix = 1
        unique_id = disc_id
        while unique_id in seen_ids:
            unique_id = f"{disc_id}-{suffix}"
            suffix += 1
        seen_ids.add(unique_id)

        private_fields.append(
            PrivateFieldRef(
                span_start=redacted_so_far_len,
                span_end=redacted_so_far_len + len(placeholder),
                disclosure_hash=disclosure_hash,
            )
        )
        disclosures.append(
            Disclosure(
                disclosure_id=unique_id,
                span_value=value,
                salt_hex=span_salt.hex(),
                disclosure_hash=disclosure_hash,
            )
        )

    out_parts.append(clause_text[cursor:])
    return "".join(out_parts), private_fields, disclosures


def verify_disclosure(disclosure: Disclosure, claimed_hash: str) -> bool:
    """True iff ``SHA-256(salt || span_value)`` reproduces ``claimed_hash``.

    Callers should pass the ``disclosure_hash`` from the matching
    `PrivateFieldRef`. Tampering with `span_value`, `salt_hex`, or
    `disclosure_hash` in the served Disclosure flips this to False.
    Returns False on any decoding error rather than raising — this is
    a verification primitive, the caller's outer logic decides what
    a False means (refuse to delegate, log a tampering alert, etc).
    """
    try:
        salt = bytes.fromhex(disclosure.salt_hex)
    except ValueError:
        return False
    recomputed = _hash_value(salt, disclosure.span_value)
    return recomputed == claimed_hash and recomputed == disclosure.disclosure_hash


def match_redacted(
    clause_text: str,
    candidate_value: str,
    disclosures: list[Disclosure],
) -> bool:
    """True iff ``candidate_value`` is the plaintext behind any redacted
    span in this clause.

    Designed for the caller-side "does this charter pertain to customer
    Foo?" check. The caller's grader LLM looks at the redacted text +
    clause structure to estimate hit confidence; if it wants to confirm
    a specific candidate against the redacted spans, it calls this and
    gets a bool. The function deliberately does NOT return WHICH
    disclosure matched — only the boolean — so a caller that probes
    with many candidates cannot enumerate the full disclosure set
    through a side channel.

    The match is anchored to a placeholder appearing in ``clause_text``:
    a disclosure whose hash placeholder is NOT present in the clause
    can never produce a positive match here, even if a caller passes
    its plaintext. This prevents a caller from accidentally querying
    "did clause C-002 mention customer Foo" with a disclosure from an
    unrelated clause.
    """
    for disc in disclosures:
        # Skip disclosures that don't belong to this clause (no
        # matching placeholder in the redacted text).
        placeholder = _placeholder_for(disc.disclosure_hash)
        if placeholder not in clause_text:
            continue
        try:
            salt = bytes.fromhex(disc.salt_hex)
        except ValueError:
            continue
        if _hash_value(salt, candidate_value) == disc.disclosure_hash:
            return True
    return False


__all__ = [
    "Disclosure",
    "SALT_BYTES",
    "match_redacted",
    "redact_clause",
    "verify_disclosure",
]
