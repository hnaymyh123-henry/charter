"""Tests for `charter.chain.verify_chain` and the new chain schema fields."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from charter.chain import verify_chain
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
    SourceCommitment,
    Summary,
)
from charter.signing import (
    generate_keypair,
    public_key_to_string,
    sign_charter,
    verify_charter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed_charter(
    *,
    charter_id: str = "charter:p:a:2026-05-18",
    clauses: list[Clause] | None = None,
    parent_charter_url: str | None = None,
    attenuation_proof: AttenuationProof | None = None,
) -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
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
