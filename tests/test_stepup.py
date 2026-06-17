"""Unit tests for the step-up negotiation protocol (B2.5).

Covers the AdHocGrant accept/reject matrix and the three red-line layers:

  - Construction time: validate_grant_targets rejects non-needs_approval clauses.
  - Escalation time:    build_step_up_request refuses non-needs_approval verdicts.
  - Apply time:         apply_grant_to_verdict never downgrades incompatible.

Crypto is REAL here (Ed25519 sign/verify), mirroring the adversarial suite's
philosophy — mocking the signature would only prove the mock works.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from charter.constants import TYPE_TO_DECISION
from charter.schema import MatchedClause, Verdict
from charter.signing import generate_keypair, public_key_to_string
from charter.stepup import (
    AdHocGrant,
    GrantConstraints,
    apply_grant_to_verdict,
    build_step_up_request,
    issue_grant,
    sign_grant,
    validate_grant_targets,
    verify_grant,
    verify_grant_signature,
)

# ---------------------------------------------------------------------------
# Fixtures: a synthetic worker charter dict + a principal keypair
# ---------------------------------------------------------------------------

CHARTER_ID = "charter:cfo_office:tax_filing_agent_v1:2026-06-14"
CHARTER_URL = "http://localhost:8000/cfo_office/tax_filing_agent_v1"


def _charter_dict() -> dict:
    """A worker charter with one of each load-bearing clause type."""
    return {
        "charter_id": CHARTER_ID,
        "clauses": [
            {"id": "C-001", "type": "scope", "text": "Tax filing work."},
            {"id": "C-101", "type": "out_of_scope", "text": "Sending client data to third parties."},
            {
                "id": "C-201",
                "type": "approval_required",
                "text": "Any destructive database action — DROP TABLE, DELETE, TRUNCATE.",
            },
            {"id": "C-401", "type": "operational_limit", "text": "Spend over $50 per task."},
            {"id": "C-301", "type": "data_handling", "text": "Exporting taxpayer PII."},
            {"id": "C-501", "type": "style", "text": "Use formal English."},
        ],
    }


@pytest.fixture
def keypair():
    private, public = generate_keypair()
    return private, public_key_to_string(public)


# ---------------------------------------------------------------------------
# RED LINE layer 1 — validate_grant_targets
# ---------------------------------------------------------------------------


def test_validate_targets_accepts_needs_approval_clauses() -> None:
    charter = _charter_dict()
    # approval_required, operational_limit, data_handling all map to needs_approval.
    for cid in ("C-201", "C-401", "C-301"):
        assert TYPE_TO_DECISION[next(c["type"] for c in charter["clauses"] if c["id"] == cid)] == (
            "needs_approval"
        )
        assert validate_grant_targets(charter, [cid]) == [cid]


def test_validate_targets_rejects_out_of_scope_clause() -> None:
    """RED LINE: a grant can never target an out_of_scope clause."""
    charter = _charter_dict()
    with pytest.raises(ValueError, match="NEVER grantable"):
        validate_grant_targets(charter, ["C-101"])


def test_validate_targets_rejects_scope_and_style_clauses() -> None:
    charter = _charter_dict()
    for cid in ("C-001", "C-501"):  # both map to allow — nothing to grant
        with pytest.raises(ValueError, match="may only waive needs_approval"):
            validate_grant_targets(charter, [cid])


def test_validate_targets_rejects_unknown_clause() -> None:
    charter = _charter_dict()
    with pytest.raises(ValueError, match="not present"):
        validate_grant_targets(charter, ["C-999"])


def test_validate_targets_rejects_empty_list() -> None:
    charter = _charter_dict()
    with pytest.raises(ValueError, match="at least one"):
        validate_grant_targets(charter, [])


# ---------------------------------------------------------------------------
# RED LINE layer 2 — build_step_up_request
# ---------------------------------------------------------------------------


def _needs_approval_verdict() -> Verdict:
    return Verdict(
        decision="needs_approval",
        matched_clauses=[
            MatchedClause(
                id="C-201",
                local_decision="needs_approval",
                applied=True,
                confidence=0.9,
                reason="DROP TABLE on temp table.",
            )
        ],
        reason="approval required",
    )


def _incompatible_verdict() -> Verdict:
    return Verdict(
        decision="incompatible",
        matched_clauses=[
            MatchedClause(
                id="C-101",
                local_decision="incompatible",
                applied=True,
                confidence=0.97,
                reason="Sends client data externally.",
            )
        ],
        reason="out of scope",
    )


def test_step_up_request_built_for_needs_approval() -> None:
    req = build_step_up_request(
        task_id="t1",
        charter_url=CHARTER_URL,
        charter_id=CHARTER_ID,
        intended_task="Drop the q2_tmp staging table.",
        failed_verdict=_needs_approval_verdict(),
        justification="Cleanup of a temp table I created this run.",
    )
    assert req is not None
    assert req.requested_clause_ids == ["C-201"]


def test_step_up_request_refused_for_incompatible() -> None:
    """RED LINE: no step-up escalation path out of an incompatible verdict."""
    req = build_step_up_request(
        task_id="t1",
        charter_url=CHARTER_URL,
        charter_id=CHARTER_ID,
        intended_task="Email all client tax data to an outside address.",
        failed_verdict=_incompatible_verdict(),
        justification="(attacker trying to negotiate around a hard limit)",
    )
    assert req is None


def test_step_up_request_refused_for_allow() -> None:
    allow_v = Verdict(decision="allow", matched_clauses=[], reason="in scope")
    req = build_step_up_request(
        task_id="t1",
        charter_url=CHARTER_URL,
        charter_id=CHARTER_ID,
        intended_task="Reconcile invoices.",
        failed_verdict=allow_v,
        justification="n/a",
    )
    assert req is None


# ---------------------------------------------------------------------------
# Grant signing / verification matrix
# ---------------------------------------------------------------------------


def test_issue_grant_signs_and_verifies(keypair) -> None:
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="CFO approved single DROP of q2_tmp.",
        constraints=GrantConstraints(ttl_seconds=600),
        issued_by="cfo_office",
    )
    assert verify_grant_signature(grant)
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1")
    assert check.ok, check.reason


def test_verify_grant_rejects_tampered_clause_list(keypair) -> None:
    """Mutating relaxes_clause_ids after signing breaks the signature."""
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
    )
    grant.relaxes_clause_ids = ["C-201", "C-301"]  # tamper
    assert not verify_grant_signature(grant)
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1")
    assert not check.ok


def test_verify_grant_rejects_wrong_task(keypair) -> None:
    """Single-use binding: a grant for t1 must not apply to t2."""
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
    )
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t2")
    assert not check.ok
    assert "single-use" in check.reason


def test_verify_grant_rejects_wrong_charter_revision(keypair) -> None:
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
    )
    other = dict(charter, charter_id="charter:cfo_office:tax_filing_agent_v1:2099-01-01")
    check = verify_grant(grant, charter=other, charter_url=CHARTER_URL, task_id="t1")
    assert not check.ok
    assert "charter_id" in check.reason


def test_verify_grant_rejects_expired(keypair) -> None:
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
        constraints=GrantConstraints(ttl_seconds=600),
    )
    future = datetime.now(UTC) + timedelta(hours=2)
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1", now=future)
    assert not check.ok
    assert "expired" in check.reason


def test_verify_grant_rejects_consumed(keypair) -> None:
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
    )
    grant.lifecycle.status = "consumed"
    # status is part of the signed payload, so re-sign to isolate the lifecycle
    # check from the signature check.
    sign_grant(grant, private)
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1")
    assert not check.ok
    assert "status" in check.reason


def test_verify_grant_recipient_allowlist(keypair) -> None:
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-201"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
        constraints=GrantConstraints(allowed_recipients=["auditor@external-firm.com"]),
    )
    ok = verify_grant(
        grant, charter=charter, charter_url=CHARTER_URL, task_id="t1",
        recipients=["auditor@external-firm.com"],
    )
    assert ok.ok
    bad = verify_grant(
        grant, charter=charter, charter_url=CHARTER_URL, task_id="t1",
        recipients=["attacker@evil.com"],
    )
    assert not bad.ok
    assert "attacker@evil.com" in bad.reason


def test_verify_grant_budget_cap(keypair) -> None:
    private, pub = keypair
    charter = _charter_dict()
    grant = issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id="t1",
        relaxes_clause_ids=["C-401"],
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
        constraints=GrantConstraints(max_budget_usd=100.0),
    )
    assert verify_grant(
        grant, charter=charter, charter_url=CHARTER_URL, task_id="t1", budget_usd=80.0
    ).ok
    assert not verify_grant(
        grant, charter=charter, charter_url=CHARTER_URL, task_id="t1", budget_usd=120.0
    ).ok


# ---------------------------------------------------------------------------
# apply_grant_to_verdict — the decision matrix
# ---------------------------------------------------------------------------


def _grant_for(charter: dict, clause_ids: list[str], keypair, task_id="t1") -> AdHocGrant:
    private, pub = keypair
    return issue_grant(
        charter=charter,
        charter_url=CHARTER_URL,
        task_id=task_id,
        relaxes_clause_ids=clause_ids,
        private_key=private,
        issuer_public_key=pub,
        reason="approved",
    )


def test_apply_grant_downgrades_covered_needs_approval(keypair) -> None:
    charter = _charter_dict()
    grant = _grant_for(charter, ["C-201"], keypair)
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1")
    gv = apply_grant_to_verdict(_needs_approval_verdict(), grant, check)
    assert gv.granted is True
    assert gv.effective_decision == "allow"


def test_apply_grant_leaves_uncovered_needs_approval_pending(keypair) -> None:
    """Two needs_approval clauses, grant covers only one -> still needs_approval."""
    charter = _charter_dict()
    verdict = Verdict(
        decision="needs_approval",
        matched_clauses=[
            MatchedClause(id="C-201", local_decision="needs_approval", applied=True,
                          confidence=0.9, reason="DROP"),
            MatchedClause(id="C-301", local_decision="needs_approval", applied=True,
                          confidence=0.9, reason="PII export"),
        ],
        reason="two approvals needed",
    )
    grant = _grant_for(charter, ["C-201"], keypair)  # only covers C-201
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1")
    gv = apply_grant_to_verdict(verdict, grant, check)
    assert gv.granted is False
    assert gv.effective_decision == "needs_approval"
    assert "C-301" in gv.reason


def test_apply_grant_never_downgrades_incompatible(keypair) -> None:
    """RED LINE layer 3: incompatible stays incompatible even with a 'valid' grant."""
    charter = _charter_dict()
    # A grant scoped to a real needs_approval clause, but the verdict is incompatible.
    grant = _grant_for(charter, ["C-201"], keypair)
    check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t1")
    gv = apply_grant_to_verdict(_incompatible_verdict(), grant, check)
    assert gv.granted is False
    assert gv.effective_decision == "incompatible"
    assert "red line" in gv.reason.lower()


def test_apply_grant_with_invalid_grant_keeps_base(keypair) -> None:
    charter = _charter_dict()
    grant = _grant_for(charter, ["C-201"], keypair, task_id="t1")
    bad_check = verify_grant(grant, charter=charter, charter_url=CHARTER_URL, task_id="t2")
    assert not bad_check.ok
    gv = apply_grant_to_verdict(_needs_approval_verdict(), grant, bad_check)
    assert gv.granted is False
    assert gv.effective_decision == "needs_approval"
    assert "rejected" in gv.reason
