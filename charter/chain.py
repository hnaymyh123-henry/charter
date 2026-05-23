"""Charter Chain — multi-hop scope attenuation.

A Charter Chain is a sequence of Charters where each child claims to be a
*stricter* version of its parent. The chain enforces a simple invariant:

    every restriction in the parent must persist or tighten in the child;
    the child must not relax anything the parent forbade.

This is the agent-as-principal case. A corporate principal issues a
broad Charter to an assistant agent; the assistant in turn issues a
narrower Charter to a sub-agent it delegates to. Attenuation guarantees
the sub-agent cannot do anything the original human/org principal
forbade, even though the assistant is technically the issuer of the
sub-Charter.

Two verification modes are provided:

  - String mode (`verify_chain` default, ADR-010): clause text is
    compared by exact match or substring containment. Zero LLM cost,
    fully deterministic, but conservative — a reworded clause that
    means the same thing won't be recognized.

  - Semantic mode (`verify_chain_semantic`): a small LLM grader judges
    whether the child clauses semantically cover each parent
    restriction. Verdicts are cached on the child's `attenuation_proof`
    and the child is re-signed so subsequent calls return the same
    bool without invoking the LLM (determinism preserved at the
    Charter-revision level).

`verify_chain(..., mode="auto")` runs the cheap string check first and
falls back to semantic only when string rejects. This is the recommended
default for callers that have an LLM available; it keeps the common case
free of LLM calls without losing reword tolerance.

The MCP-callable form that walks a chain over the network is in
`mcp_server.fetch_charter_chain`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ._logging import get_logger
from .errors import CharterChainGraderError
from .observability import charter_span_cm, set_span_attrs
from .schema import AttenuationProof, Charter, Clause, SemanticCheckResult

_log = get_logger("charter.chain")

Mode = Literal["strict", "semantic", "auto"]


# ---------------------------------------------------------------------------
# Grader client protocol
# ---------------------------------------------------------------------------


class GraderClient(Protocol):
    """Minimal duck-typed interface for the LLM grader.

    Anthropic's official `anthropic.Anthropic()` satisfies this via its
    `.messages.create(...)` method. Tests pass in a fake with the same
    shape — no real network call.
    """

    @property
    def messages(self) -> Any: ...  # pragma: no cover - structural typing only


# ---------------------------------------------------------------------------
# Public API — string-based (ADR-010 fallback, default)
# ---------------------------------------------------------------------------


def verify_chain(
    child: Charter,
    parent: Charter,
    *,
    mode: Mode = "strict",
    grader_client: GraderClient | None = None,
    signer_private_key: Ed25519PrivateKey | None = None,
) -> bool:
    """True iff `child` is a valid attenuation of `parent`.

    Args:
        child:   The (claimed) attenuating Charter.
        parent:  The Charter `child` should be a subset of.
        mode:    Verification mode.
            - `"strict"` (default, back-compat with pre-A1 callers):
                pure string equality / containment. No LLM ever invoked,
                so `grader_client` is ignored.
            - `"semantic"`: skip the string check and go straight to the
                LLM grader. `grader_client` is required.
            - `"auto"`: run `"strict"` first; on failure, fall back to
                `"semantic"`. The string path catches the cheap obvious
                cases without burning LLM budget; the semantic path
                tolerates rewords. `grader_client` is required.
        grader_client:
            LLM client used by `"semantic"` / `"auto"` modes. Optional in
            `"strict"`. The fallback shipped with the package builds an
            `anthropic.Anthropic()` if you pass `None` while in
            `"semantic"` / `"auto"` — but tests should always inject a
            fake.
        signer_private_key:
            When semantic verification produces a fresh cache entry, the
            child Charter is re-signed so the cached verdict travels with
            the Charter and is itself signed (no unsigned trust). Pass
            the child's issuer private key here; if `None`, the cache is
            still updated in-memory but the signature is NOT refreshed
            (a WARN log records this and the caller is responsible for
            persisting / re-signing). MCP server tools deliberately call
            with `None` because they don't hold issuer keys.

    Rules (strict mode):

      1. **`out_of_scope` is preserved or expanded.** Every
         `out_of_scope` clause in `parent` must be covered by some
         `out_of_scope` clause in `child` (text equality or child's text
         being a superstring of the parent's). Child may add new
         exclusions freely.

      2. **`approval_required` is preserved or expanded.** Same rule as
         out_of_scope.

      3. **`scope` is a subset.** Every `scope` clause in `child` must
         match some `scope` clause in `parent` by exact text equality
         (so the child cannot grant itself a capability the parent
         did not authorize). Child may have FEWER scope clauses than
         parent; that's fine — narrower scope is the whole point.

      4. **`attenuation_proof.parent_charter_id` matches** the actual
         parent's `charter_id` when present. The proof is optional, but
         when supplied it has to be honest.

    Non-rules:

      - `operational_limit`, `style`, `data_handling` are not checked.
        A future iteration may add per-type rules; the conservative
        answer for now is that these don't gate attenuation.

    Emits one `charter.chain` log line per call with the outcome.

    Raises:
        CharterChainGraderError: only in `"semantic"` / `"auto"` modes,
            if the grader fails (network, parse, etc.). String-mode never
            raises this.
        ValueError: invalid `mode`, or `"semantic"` / `"auto"` requested
            without a `grader_client` and no `ANTHROPIC_API_KEY` to build
            one.
    """
    with charter_span_cm(
        "charter.verify_chain",
        {
            "charter.id": child.charter_id,
            "charter.parent_id": parent.charter_id,
            "charter.mode": mode,
        },
    ) as span:
        if mode == "strict":
            ok = _verify_chain_strict(child, parent)
            set_span_attrs(span, {"charter.verdict": "ok" if ok else "rejected"})
            return ok

        if mode == "semantic":
            ok = verify_chain_semantic(
                child,
                parent,
                grader_client=_resolve_grader(grader_client),
                signer_private_key=signer_private_key,
            )
            set_span_attrs(span, {"charter.verdict": "ok" if ok else "rejected"})
            return ok

        if mode == "auto":
            if _verify_chain_strict(child, parent):
                set_span_attrs(span, {"charter.verdict": "ok", "charter.via": "strict"})
                return True
            # String path rejected. Either the child genuinely relaxes
            # something OR it just rewords the parent's clauses. Fall back
            # to the LLM grader to disambiguate.
            _log.info(
                "string check failed; falling back to semantic grader",
                extra={
                    "child_charter_id": child.charter_id,
                    "parent_charter_id": parent.charter_id,
                    "outcome": "auto_fallback",
                },
            )
            ok = verify_chain_semantic(
                child,
                parent,
                grader_client=_resolve_grader(grader_client),
                signer_private_key=signer_private_key,
            )
            set_span_attrs(
                span, {"charter.verdict": "ok" if ok else "rejected", "charter.via": "semantic"}
            )
            return ok

        set_span_attrs(span, {"charter.verdict": "unknown_mode"})
        raise ValueError(f"verify_chain: unknown mode {mode!r}")


def _verify_chain_strict(child: Charter, parent: Charter) -> bool:
    """The pre-A1 string-based verifier. Preserved verbatim per ADR-010."""
    parent_id = parent.charter_id
    child_id = child.charter_id

    # 1. attenuation_proof.parent_charter_id check
    if child.attenuation_proof is not None:
        claimed_parent = child.attenuation_proof.parent_charter_id
        if claimed_parent != parent_id:
            _log.warning(
                "chain rejected: attenuation_proof.parent_charter_id mismatch",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "claimed_parent": claimed_parent,
                    "outcome": "proof_mismatch",
                },
            )
            return False

    # 2. out_of_scope coverage
    parent_oos = [c for c in parent.clauses if c.type == "out_of_scope"]
    child_oos = [c for c in child.clauses if c.type == "out_of_scope"]
    for pc in parent_oos:
        if not _any_covers(child_oos, pc):
            _log.warning(
                "chain rejected: child does not preserve parent's out_of_scope",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "uncovered_clause_id": pc.id,
                    "uncovered_clause_text": pc.text,
                    "outcome": "out_of_scope_relaxed",
                },
            )
            return False

    # 3. approval_required coverage
    parent_appr = [c for c in parent.clauses if c.type == "approval_required"]
    child_appr = [c for c in child.clauses if c.type == "approval_required"]
    for pc in parent_appr:
        if not _any_covers(child_appr, pc):
            _log.warning(
                "chain rejected: child does not preserve parent's approval_required",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "uncovered_clause_id": pc.id,
                    "uncovered_clause_text": pc.text,
                    "outcome": "approval_required_relaxed",
                },
            )
            return False

    # 4. scope subset
    parent_scope_texts = {c.text for c in parent.clauses if c.type == "scope"}
    for cc in child.clauses:
        if cc.type == "scope" and cc.text not in parent_scope_texts:
            _log.warning(
                "chain rejected: child has a scope clause not present in parent",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "extra_clause_id": cc.id,
                    "extra_clause_text": cc.text,
                    "outcome": "scope_widened",
                },
            )
            return False

    _log.info(
        "chain verified",
        extra={
            "child_charter_id": child_id,
            "parent_charter_id": parent_id,
            "outcome": "ok",
        },
    )
    return True


# ---------------------------------------------------------------------------
# Public API — semantic (LLM-based, A1)
# ---------------------------------------------------------------------------


def verify_chain_semantic(
    child: Charter,
    parent: Charter,
    *,
    grader_client: GraderClient,
    signer_private_key: Ed25519PrivateKey | None = None,
) -> bool:
    """LLM-based semantic subset check.

    Per parent restriction (out_of_scope + approval_required), ask the
    grader whether the child's clauses of the same type semantically
    cover it. The chain passes iff every parent restriction is covered.
    Scope clauses are checked one-at-a-time in the other direction:
    every child `scope` clause must be covered by some parent scope.

    Results are memoized on `child.attenuation_proof.semantic_check_cache`
    keyed by `f"{parent.charter_id}@{parent.lifecycle.issued_at}"`. The
    `issued_at` component means any re-sign of the parent invalidates
    every cached verdict against it.

    Args:
        child:               Claimed attenuating Charter. MAY be mutated
                             (its `attenuation_proof.semantic_check_cache`
                             grows, and its signature is refreshed if
                             `signer_private_key` is provided).
        parent:              The Charter `child` is being checked against.
        grader_client:       The LLM client.
        signer_private_key:  See `verify_chain` docstring.

    Returns:
        True iff every parent restriction is covered AND no child scope
        clause extends beyond the parent's scope.

    Raises:
        CharterChainGraderError: grader unreachable, timed out, or
            returned non-JSON / JSON missing required fields.
    """
    parent_id = parent.charter_id
    child_id = child.charter_id

    # 1. attenuation_proof.parent_charter_id check — semantic mode keeps
    # this guard. If the child explicitly claims a different parent, no
    # amount of semantic reasoning can rescue it.
    if child.attenuation_proof is not None:
        claimed_parent = child.attenuation_proof.parent_charter_id
        if claimed_parent != parent_id:
            _log.warning(
                "semantic chain rejected: attenuation_proof.parent_charter_id mismatch",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "claimed_parent": claimed_parent,
                    "outcome": "proof_mismatch",
                },
            )
            return False

    # 2. Ensure child carries an attenuation_proof so we have a place to
    # write the cache. Auto-create one pointing at the actual parent if
    # missing — this is safe because the previous block already enforced
    # parent_id consistency.
    if child.attenuation_proof is None:
        child.attenuation_proof = AttenuationProof(parent_charter_id=parent_id)

    proof = child.attenuation_proof
    cache_key = _semantic_cache_key(parent)
    cache_dirty = False

    if cache_key in proof.semantic_check_cache:
        cached = proof.semantic_check_cache[cache_key]
        _log.info(
            "semantic chain decided from cache",
            extra={
                "child_charter_id": child_id,
                "parent_charter_id": parent_id,
                "cache_key": cache_key,
                "outcome": "ok" if cached.matches_subset else "cached_failure",
            },
        )
        return cached.matches_subset

    # 3. Run the per-clause grader.
    parent_restrictions = [
        c for c in parent.clauses if c.type in ("out_of_scope", "approval_required")
    ]
    child_oos = [c for c in child.clauses if c.type == "out_of_scope"]
    child_appr = [c for c in child.clauses if c.type == "approval_required"]
    parent_scopes = [c for c in parent.clauses if c.type == "scope"]
    child_scopes = [c for c in child.clauses if c.type == "scope"]

    overall_match = True
    overall_reason = "all parent restrictions semantically covered"

    # Parent restrictions: each must be covered by the child set.
    for parent_clause in parent_restrictions:
        candidates = child_oos if parent_clause.type == "out_of_scope" else child_appr
        verdict = _grade_one(grader_client, parent_clause, candidates, direction="restriction")
        if not verdict.matches_subset:
            overall_match = False
            overall_reason = (
                f"parent {parent_clause.type} clause {parent_clause.id!r} "
                f"not covered by child: {verdict.reason}"
            )
            break

    # Child scope clauses: each must be covered by some parent scope.
    if overall_match:
        for child_clause in child_scopes:
            verdict = _grade_one(
                grader_client, child_clause, parent_scopes, direction="scope_authorized_by"
            )
            if not verdict.matches_subset:
                overall_match = False
                overall_reason = (
                    f"child scope clause {child_clause.id!r} extends beyond "
                    f"parent scope: {verdict.reason}"
                )
                break

    # 4. Cache + (optionally) re-sign.
    result = SemanticCheckResult(
        matches_subset=overall_match,
        reason=overall_reason,
        graded_at=datetime.now(UTC).replace(microsecond=0),
    )
    proof.semantic_check_cache[cache_key] = result
    cache_dirty = True

    if cache_dirty and signer_private_key is not None:
        # Lazy import to avoid a circular dependency between chain and
        # signing/transparency.
        from . import signing

        # Clear the existing signature so sign_charter rebuilds it from the
        # new canonical bytes (which now include the cache entry).
        child.provenance.issuer_signature = ""
        signing.sign_charter(child, signer_private_key)
    elif cache_dirty:
        _log.warning(
            "semantic verdict cached in memory but child Charter not re-signed "
            "(no signer_private_key supplied); caller must persist + re-sign",
            extra={
                "child_charter_id": child_id,
                "parent_charter_id": parent_id,
                "outcome": "cache_unsigned",
            },
        )

    _log.info(
        "semantic chain verified" if overall_match else "semantic chain rejected",
        extra={
            "child_charter_id": child_id,
            "parent_charter_id": parent_id,
            "cache_key": cache_key,
            "outcome": "ok" if overall_match else "semantic_failure",
            "reason": overall_reason,
        },
    )
    return overall_match


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _covers(child_clause: Clause, parent_clause: Clause) -> bool:
    """True iff `child_clause` is at least as restrictive as `parent_clause`.

    String-mode rule: text equality OR child's text is a superstring of
    parent's. Both are case-sensitive. The superstring rule lets a child
    say "Do not accept marketing copy OR cold-email campaigns" and have
    that count as covering parent's "Do not accept marketing copy".
    """
    pt = parent_clause.text.strip()
    ct = child_clause.text.strip()
    return pt == ct or pt in ct


def _any_covers(child_clauses: list[Clause], parent_clause: Clause) -> bool:
    return any(_covers(cc, parent_clause) for cc in child_clauses)


def _semantic_cache_key(parent: Charter) -> str:
    """Stable cache key for one (parent_id, parent_revision) pair.

    Uses `issued_at.isoformat()` so any re-sign of the parent — which
    bumps issued_at — invalidates every cached verdict against it.
    """
    return f"{parent.charter_id}@{parent.lifecycle.issued_at.isoformat()}"


def _grade_one(
    grader_client: GraderClient,
    target_clause: Clause,
    candidate_clauses: list[Clause],
    *,
    direction: Literal["restriction", "scope_authorized_by"],
) -> SemanticCheckResult:
    """Ask the grader to judge one (target, candidates) pair.

    `direction` selects the framing in the user message so the LLM
    knows whether `target_clause` is the parent restriction (children
    must cover) or a child scope clause (parents must authorize).
    """
    from .constants import DEFAULT_MODEL
    from .prompts import CHAIN_SEMANTIC_GRADER_SYSTEM

    if direction == "restriction":
        framing = (
            f"PARENT clause (must be preserved or tightened by child):\n"
            f"  - id: {target_clause.id}\n"
            f"  - type: {target_clause.type}\n"
            f"  - text: {target_clause.text}\n\n"
            f"CHILD candidate clauses (same type, any subset may cover it):\n"
        )
    else:
        framing = (
            f"CHILD scope clause (must be authorized by parent scope):\n"
            f"  - id: {target_clause.id}\n"
            f"  - type: scope\n"
            f"  - text: {target_clause.text}\n\n"
            f"PARENT scope clauses (any one may authorize it):\n"
        )

    if not candidate_clauses:
        framing += "  (no candidate clauses on the other side)\n"
    else:
        for c in candidate_clauses:
            framing += f"  - id: {c.id}, text: {c.text}\n"

    model = os.environ.get("CHARTER_MODEL", DEFAULT_MODEL)

    try:
        message = grader_client.messages.create(
            model=model,
            max_tokens=512,
            temperature=0.0,
            system=CHAIN_SEMANTIC_GRADER_SYSTEM,
            messages=[{"role": "user", "content": framing}],
        )
    except Exception as e:
        raise CharterChainGraderError(
            f"semantic grader call failed for clause {target_clause.id!r}: {e}"
        ) from e

    text = _extract_text(message)
    text = _strip_markdown_fences(text)
    if not text:
        raise CharterChainGraderError(
            f"semantic grader returned empty output for clause {target_clause.id!r}"
        )

    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as e:
        raise CharterChainGraderError(
            f"semantic grader returned non-JSON for clause {target_clause.id!r}: {e}"
        ) from e

    if not isinstance(data, dict) or "matches_subset" not in data or "reason" not in data:
        raise CharterChainGraderError(
            f"semantic grader output missing required keys for clause {target_clause.id!r}: "
            f"{data!r}"
        )
    if not isinstance(data["matches_subset"], bool) or not isinstance(data["reason"], str):
        raise CharterChainGraderError(
            f"semantic grader output has wrong types for clause {target_clause.id!r}: {data!r}"
        )

    return SemanticCheckResult(
        matches_subset=data["matches_subset"],
        reason=data["reason"],
        graded_at=datetime.now(UTC).replace(microsecond=0),
    )


def _extract_text(message: Any) -> str:
    """Pull the text out of an Anthropic-style message object.

    Mirrors the helper used by `charter.propose` so any client whose
    `.content` is a list of blocks with `.type == "text"` and `.text`
    fields works without modification.
    """
    content = getattr(message, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :].lstrip()
    return text


def _resolve_grader(grader_client: GraderClient | None) -> GraderClient:
    """Return the explicit grader, or build an `anthropic.Anthropic()`
    from env if the caller passed `None`.

    Tests should always inject. This fallback exists so the MCP tool
    layer can call without plumbing an extra argument when an API key
    is configured.
    """
    if grader_client is not None:
        return grader_client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError(
            "verify_chain semantic mode requires either a grader_client argument "
            "or ANTHROPIC_API_KEY in the environment"
        )
    import anthropic

    return anthropic.Anthropic()
