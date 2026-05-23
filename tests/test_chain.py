"""Tests for `charter.chain.verify_chain` and the new chain schema fields."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from charter.chain import verify_chain, verify_chain_semantic
from charter.errors import CharterChainGraderError
from charter.schema import (
    AgentOperator,
    AttenuationProof,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    MatchedClause,
    Principal,
    Provenance,
    SemanticCheckResult,
    SourceCommitment,
    Summary,
)
from charter.signing import (
    Ed25519PrivateKey,
    generate_keypair,
    public_key_to_string,
    sign_charter,
    verify_charter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signed_charter(
    *,
    charter_id: str = "charter:p:a:2026-05-18",
    clauses: list[Clause] | None = None,
    parent_charter_url: str | None = None,
    attenuation_proof: AttenuationProof | None = None,
    issued_at: datetime | None = None,
) -> tuple[Charter, Ed25519PrivateKey]:
    """Build + sign a Charter. Returns (charter, private_key) so callers
    that need to re-sign (semantic-cache writeback) can do so."""
    private, public = generate_keypair()
    now = (issued_at or datetime.now(UTC)).replace(microsecond=0)
    charter = Charter(
        charter_id=charter_id,
        binding=Binding(principal_id="p", agent_id="a"),
        principal=Principal(id="p", role_summary="Test"),
        issuer=Issuer(id="p"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=clauses or [Clause(id="C-001", type="scope", text="anything")],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="t",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
        parent_charter_url=parent_charter_url,
        attenuation_proof=attenuation_proof,
    )
    sign_charter(charter, private)
    return charter, private


def _signed_charter(
    *,
    charter_id: str = "charter:p:a:2026-05-18",
    clauses: list[Clause] | None = None,
    parent_charter_url: str | None = None,
    attenuation_proof: AttenuationProof | None = None,
) -> Charter:
    """Back-compat wrapper for existing tests that only need the Charter."""
    charter, _ = _make_signed_charter(
        charter_id=charter_id,
        clauses=clauses,
        parent_charter_url=parent_charter_url,
        attenuation_proof=attenuation_proof,
    )
    return charter


def _scope(text: str, cid: str = "C-001") -> Clause:
    return Clause(id=cid, type="scope", text=text)


def _oos(text: str, cid: str = "C-002") -> Clause:
    return Clause(id=cid, type="out_of_scope", text=text)


def _appr(text: str, cid: str = "C-003") -> Clause:
    return Clause(id=cid, type="approval_required", text=text)


# ---------------------------------------------------------------------------
# Schema additions — back-compat + signing round-trip
# ---------------------------------------------------------------------------


def test_root_charter_has_no_parent_fields() -> None:
    """Existing single-Charter callers should see None for both new fields."""
    c = _signed_charter()
    assert c.parent_charter_url is None
    assert c.attenuation_proof is None


def test_chained_charter_signing_round_trip() -> None:
    """The new fields are covered by canonical_bytes — i.e. signing still
    verifies even when parent_charter_url / attenuation_proof are set."""
    child = _signed_charter(
        charter_id="child:1",
        parent_charter_url="https://parent.example.com/p/a",
        attenuation_proof=AttenuationProof(
            parent_charter_id="parent:1",
            attenuates={"C-002": ["C-002"]},
        ),
    )
    assert verify_charter(child) is True


def test_attenuation_proof_roundtrips_through_model_dump() -> None:
    proof = AttenuationProof(
        parent_charter_id="parent:1",
        attenuates={"C-002": ["C-002", "C-005"]},
    )
    restored = AttenuationProof.model_validate(proof.model_dump())
    assert restored == proof


def test_matched_clause_source_charter_id_defaults_to_none() -> None:
    """Existing aggregate_verdict callers still produce records without
    source_charter_id (back-compat for the single-Charter path)."""
    mc = MatchedClause(
        id="C-001",
        local_decision="allow",
        applied=True,
        confidence=0.9,
        reason="test",
    )
    assert mc.source_charter_id is None


# ---------------------------------------------------------------------------
# verify_chain — out_of_scope preservation
# ---------------------------------------------------------------------------


def test_chain_valid_when_child_inherits_parents_oos() -> None:
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Accounting and tax work."),
            _oos("Do not accept marketing copy."),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Accounting and tax work."),
            _oos("Do not accept marketing copy."),
            _oos("Do not export customer PII to CSV.", cid="C-003"),  # child adds restriction
        ],
    )
    assert verify_chain(child, parent) is True


def test_chain_invalid_when_child_drops_parents_oos() -> None:
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Accounting and tax work."),
            _oos("Do not accept marketing copy."),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Accounting and tax work."),
            # Parent's "Do not accept marketing copy" is missing.
        ],
    )
    assert verify_chain(child, parent) is False


def test_chain_valid_when_child_text_is_superstring_of_parents_oos() -> None:
    """The superstring rule: if child says 'X OR Y' and parent says 'X',
    child counts as covering parent's clause."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Coding work."),
            _oos("Do not accept marketing copy."),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Coding work."),
            _oos("Do not accept marketing copy. Also no cold-email campaigns."),
        ],
    )
    assert verify_chain(child, parent) is True


# ---------------------------------------------------------------------------
# verify_chain — approval_required preservation
# ---------------------------------------------------------------------------


def test_chain_invalid_when_child_drops_parents_approval_required() -> None:
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Engineering work."),
            _appr("Production DB writes require human approval."),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Engineering work."),
            # Parent's approval_required missing.
        ],
    )
    assert verify_chain(child, parent) is False


def test_chain_valid_when_child_keeps_parents_approval_required() -> None:
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Engineering work."),
            _appr("Production DB writes require human approval."),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Engineering work."),
            _appr("Production DB writes require human approval."),
        ],
    )
    assert verify_chain(child, parent) is True


# ---------------------------------------------------------------------------
# verify_chain — scope subset
# ---------------------------------------------------------------------------


def test_chain_invalid_when_child_adds_scope_not_in_parent() -> None:
    """Child cannot grant itself a capability the parent did not authorize."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Accounting work only."),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Accounting work only."),
            _scope("Marketing work too.", cid="C-002"),  # widened — not allowed
        ],
    )
    assert verify_chain(child, parent) is False


def test_chain_valid_when_child_narrows_scope() -> None:
    """Child may have FEWER scope clauses than parent — that's
    attenuation, the whole point."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Accounting work.", cid="C-001"),
            _scope("Engineering work.", cid="C-002"),
        ],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Accounting work.", cid="C-001"),
            # Child drops engineering — narrower, still valid.
        ],
    )
    assert verify_chain(child, parent) is True


# ---------------------------------------------------------------------------
# verify_chain — attenuation_proof.parent_charter_id check
# ---------------------------------------------------------------------------


def test_chain_invalid_when_attenuation_proof_points_at_wrong_parent() -> None:
    parent = _signed_charter(
        charter_id="parent:actual",
        clauses=[_scope("anything")],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[_scope("anything")],
        attenuation_proof=AttenuationProof(parent_charter_id="parent:wrong"),
    )
    assert verify_chain(child, parent) is False


def test_chain_valid_when_attenuation_proof_matches() -> None:
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[_scope("anything")],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[_scope("anything")],
        attenuation_proof=AttenuationProof(parent_charter_id="parent:1"),
    )
    assert verify_chain(child, parent) is True


def test_chain_valid_when_no_attenuation_proof() -> None:
    """The proof is optional; absence does not fail verification on its own."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[_scope("anything")],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[_scope("anything")],
        # No attenuation_proof set.
    )
    assert verify_chain(child, parent) is True


# ---------------------------------------------------------------------------
# Outcome logging
# ---------------------------------------------------------------------------


def test_verify_chain_emits_log_on_success(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    parent = _signed_charter(charter_id="parent:1", clauses=[_scope("x")])
    child = _signed_charter(charter_id="child:1", clauses=[_scope("x")])

    with caplog.at_level(logging.INFO, logger="charter.chain"):
        verify_chain(child, parent)

    rec = next(r for r in caplog.records if r.name == "charter.chain")
    assert rec.outcome == "ok"
    assert rec.parent_charter_id == "parent:1"
    assert rec.child_charter_id == "child:1"


def test_verify_chain_emits_log_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[_oos("Do not write code.")],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[],  # missing the oos
    )

    with caplog.at_level(logging.WARNING, logger="charter.chain"):
        verify_chain(child, parent)

    rec = next(r for r in caplog.records if r.name == "charter.chain")
    assert rec.outcome == "out_of_scope_relaxed"


# ---------------------------------------------------------------------------
# A1: semantic subset verification (LLM-based)
# ---------------------------------------------------------------------------


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]


class _FakeMessages:
    """Stand-in for `anthropic.Anthropic().messages`.

    The grader calls `messages.create(...)` once per (parent_clause,
    child_candidates) pair, so tests can drive the loop by providing a
    list of canned response strings. `raise_on_call` flips it into "any
    call blows up" mode for the grader-failure case.
    """

    def __init__(self, responses: list[str], *, raise_on_call: Exception | None = None) -> None:
        self._responses = list(responses)
        self._raise = raise_on_call
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        if not self._responses:
            raise AssertionError("FakeMessages: no canned response left for call")
        return _FakeMessage(self._responses.pop(0))


class _FakeGrader:
    def __init__(self, responses: list[str], *, raise_on_call: Exception | None = None) -> None:
        self.messages = _FakeMessages(responses, raise_on_call=raise_on_call)


def _match_response(reason: str = "covered") -> str:
    return f'{{"matches_subset": true, "reason": "{reason}"}}'


def _miss_response(reason: str = "not covered") -> str:
    return f'{{"matches_subset": false, "reason": "{reason}"}}'


def test_strict_mode_passes_when_text_matches() -> None:
    """[AC: mode='strict'] String path hits → pass without any LLM."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[_scope("Coding."), _oos("No marketing copy.")],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[_scope("Coding."), _oos("No marketing copy.")],
    )
    grader = _FakeGrader([])  # zero responses; grader must NOT be called
    assert verify_chain(child, parent, mode="strict", grader_client=grader) is True
    assert grader.messages.calls == []


def test_auto_mode_short_circuits_on_string_pass() -> None:
    """[AC: mode='auto'] When string matches, semantic grader is skipped."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[_scope("Coding."), _oos("No marketing.")],
    )
    child = _signed_charter(
        charter_id="child:1",
        clauses=[_scope("Coding."), _oos("No marketing.")],
    )
    grader = _FakeGrader([])
    assert verify_chain(child, parent, mode="auto", grader_client=grader) is True
    assert grader.messages.calls == []


def test_auto_mode_falls_back_to_semantic_when_string_fails() -> None:
    """[AC: mode='auto'] String fails (reworded clauses) → semantic passes."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Engineering work."),
            _oos("Do not write production database migrations."),
        ],
    )
    # Reworded — string path will reject; we make the grader pass it.
    child, child_priv = _make_signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Engineering work."),
            _oos("DB migrations against production are forbidden.", cid="C-099"),
        ],
    )
    # The grader is called once per parent restriction + once per child
    # scope clause: 1 parent oos + 1 child scope = 2 calls.
    grader = _FakeGrader(
        [_match_response("reword preserves meaning"), _match_response("scope authorized")]
    )
    assert (
        verify_chain(
            child,
            parent,
            mode="auto",
            grader_client=grader,
            signer_private_key=child_priv,
        )
        is True
    )
    assert len(grader.messages.calls) == 2
    # Verdict cached on child + signature refreshed.
    assert child.attenuation_proof is not None
    assert len(child.attenuation_proof.semantic_check_cache) == 1
    assert verify_charter(child) is True


def test_semantic_verifier_returns_false_when_grader_rejects() -> None:
    """[AC: semantic failure path] grader says no → verifier returns False."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[
            _scope("Engineering."),
            _oos("Do not deploy to production."),
        ],
    )
    child, child_priv = _make_signed_charter(
        charter_id="child:1",
        clauses=[
            _scope("Engineering."),
            _oos("No prod email blasts.", cid="C-007"),  # unrelated reword
        ],
    )
    grader = _FakeGrader([_miss_response("child does not forbid prod deploy")])
    assert (
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)
        is False
    )
    # Failure ALSO gets cached — determinism applies in both directions.
    assert child.attenuation_proof is not None
    cached = next(iter(child.attenuation_proof.semantic_check_cache.values()))
    assert cached.matches_subset is False


def test_cache_hit_short_circuits_llm() -> None:
    """[AC: cache hit] Pre-populated cache → grader is NEVER called."""
    parent = _signed_charter(
        charter_id="parent:7",
        clauses=[_scope("X."), _oos("Do not Y.")],
    )
    # Pre-seed the cache with a positive verdict against parent:7's revision.
    cache_key = f"parent:7@{parent.lifecycle.issued_at.isoformat()}"
    child, child_priv = _make_signed_charter(
        charter_id="child:7",
        clauses=[_scope("X."), _oos("Other reword.")],
        attenuation_proof=AttenuationProof(
            parent_charter_id="parent:7",
            semantic_check_cache={
                cache_key: SemanticCheckResult(
                    matches_subset=True,
                    reason="seeded for test",
                    graded_at=datetime.now(UTC).replace(microsecond=0),
                )
            },
        ),
    )

    grader = _FakeGrader([])  # zero responses; any call would raise
    assert (
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)
        is True
    )
    assert grader.messages.calls == []


def test_cache_key_includes_issued_at_invalidates_on_reissue() -> None:
    """[AC: cache key] Re-signing parent (new issued_at) invalidates stale verdicts."""
    parent_v1 = _signed_charter(
        charter_id="parent:1",
        clauses=[_scope("X."), _oos("Do not Y.")],
    )

    # Seed cache against parent_v1's revision.
    cache_key_v1 = f"parent:1@{parent_v1.lifecycle.issued_at.isoformat()}"
    child, child_priv = _make_signed_charter(
        charter_id="child:1",
        clauses=[_scope("X."), _oos("Y is forbidden.")],
        attenuation_proof=AttenuationProof(
            parent_charter_id="parent:1",
            semantic_check_cache={
                cache_key_v1: SemanticCheckResult(
                    matches_subset=True,
                    reason="stale",
                    graded_at=datetime.now(UTC).replace(microsecond=0),
                )
            },
        ),
    )

    # Re-issue parent with a later issued_at — cache key changes.
    parent_v2, _ = _make_signed_charter(
        charter_id="parent:1",
        clauses=[_scope("X."), _oos("Do not Y.")],
        issued_at=parent_v1.lifecycle.issued_at + timedelta(days=1),
    )

    grader = _FakeGrader(
        [
            _match_response("reword preserves Y restriction"),
            _match_response("scope authorized"),
        ]
    )
    assert (
        verify_chain_semantic(child, parent_v2, grader_client=grader, signer_private_key=child_priv)
        is True
    )
    # The LLM was called twice (1 parent oos + 1 child scope; cache miss against v2's key).
    assert len(grader.messages.calls) == 2
    # Both cache entries now coexist on the child.
    assert child.attenuation_proof is not None
    assert len(child.attenuation_proof.semantic_check_cache) == 2


def test_grader_failure_raises_typed_exception() -> None:
    """[AC: error handling] grader exception → CharterChainGraderError."""
    parent = _signed_charter(
        charter_id="parent:1",
        clauses=[_oos("Do not Y.")],
    )
    child, child_priv = _make_signed_charter(
        charter_id="child:1",
        clauses=[_oos("Reworded Y.")],
    )
    grader = _FakeGrader([], raise_on_call=RuntimeError("network down"))
    with pytest.raises(CharterChainGraderError, match="network down"):
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)


def test_grader_returns_unparseable_json_raises() -> None:
    """[AC: error handling] non-JSON grader output → CharterChainGraderError."""
    parent = _signed_charter(charter_id="parent:1", clauses=[_oos("Do not Y.")])
    child, child_priv = _make_signed_charter(charter_id="child:1", clauses=[_oos("Reworded Y.")])
    grader = _FakeGrader(["this is not json at all"])
    with pytest.raises(CharterChainGraderError, match="non-JSON"):
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)


def test_grader_returns_json_missing_keys_raises() -> None:
    """[AC: error handling] JSON without required keys → CharterChainGraderError."""
    parent = _signed_charter(charter_id="parent:1", clauses=[_oos("Do not Y.")])
    child, child_priv = _make_signed_charter(charter_id="child:1", clauses=[_oos("Reworded Y.")])
    # Missing both `matches_subset` and `reason`.
    grader = _FakeGrader(['{"verdict": "yes"}'])
    with pytest.raises(CharterChainGraderError, match="missing required keys"):
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)


def test_grader_strips_markdown_fences() -> None:
    """The grader is tolerant of accidental ```json fences in output."""
    parent = _signed_charter(charter_id="parent:1", clauses=[_oos("Do not Y.")])
    child, child_priv = _make_signed_charter(charter_id="child:1", clauses=[_oos("Reworded Y.")])
    grader = _FakeGrader(['```json\n{"matches_subset": true, "reason": "fits"}\n```'])
    assert (
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)
        is True
    )


def test_semantic_rejects_proof_pointing_at_wrong_parent() -> None:
    """[AC: ADR-010 parent_id invariant] Proof must match parent even in
    semantic mode — no amount of LLM reasoning rescues a wrong claim."""
    parent = _signed_charter(charter_id="parent:actual", clauses=[_scope("x")])
    child, child_priv = _make_signed_charter(
        charter_id="child:1",
        clauses=[_scope("x")],
        attenuation_proof=AttenuationProof(parent_charter_id="parent:wrong"),
    )
    grader = _FakeGrader([])  # would-be call should never happen
    assert (
        verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=child_priv)
        is False
    )
    assert grader.messages.calls == []


def test_semantic_resign_when_no_private_key_warns_but_caches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without signer_private_key, the verdict still caches in memory but
    the child Charter's signature does NOT get refreshed and a WARN is
    emitted (MCP server tool path)."""
    import logging

    parent = _signed_charter(charter_id="parent:1", clauses=[_oos("Do not Y.")])
    child = _signed_charter(charter_id="child:1", clauses=[_oos("Reworded Y.")])
    pre_sig = child.provenance.issuer_signature
    grader = _FakeGrader([_match_response()])

    with caplog.at_level(logging.WARNING, logger="charter.chain"):
        result = verify_chain_semantic(child, parent, grader_client=grader, signer_private_key=None)
    assert result is True
    # Signature unchanged — caller (MCP server) cannot re-sign.
    assert child.provenance.issuer_signature == pre_sig
    # Cache populated.
    assert child.attenuation_proof is not None
    assert len(child.attenuation_proof.semantic_check_cache) == 1
    # WARN log emitted to flag the unsigned cache.
    assert any(
        r.name == "charter.chain" and getattr(r, "outcome", None) == "cache_unsigned"
        for r in caplog.records
    )


def test_verify_chain_mode_invalid_raises() -> None:
    parent = _signed_charter(charter_id="parent:1", clauses=[_scope("x")])
    child = _signed_charter(charter_id="child:1", clauses=[_scope("x")])
    with pytest.raises(ValueError, match="unknown mode"):
        verify_chain(child, parent, mode="bogus")  # type: ignore[arg-type]


def test_semantic_mode_requires_grader_or_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """semantic / auto without grader_client AND without API key → ValueError."""
    parent = _signed_charter(charter_id="parent:1", clauses=[_oos("Do not Y.")])
    child = _signed_charter(charter_id="child:1", clauses=[_oos("Other.")])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="grader_client"):
        verify_chain(child, parent, mode="semantic")
