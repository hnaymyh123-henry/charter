"""Tests for `propose_within_scope_verified` (loopback wrapper).

We stub both `propose_within_scope_llm` and `_grade_via_llm` so each test
controls exactly which attempts succeed and what the per-attempt grader
returns. No real LLM calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from charter import loopback as loopback_module
from charter.loopback import propose_within_scope_verified
from charter.mcp_server import (
    propose_within_scope_verified as _verified_tool,
)
from charter.schema import (
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    MatchedClause,
    Principal,
    Provenance,
    RewriteAttempt,
    RewriteFailure,
    RewriteProposal,
    SourceCommitment,
    Summary,
    Verdict,
)
from charter.signing import generate_keypair, public_key_to_string, sign_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _signed_charter() -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:alice@acme.com:research_agent_v1:2026-05-18",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Senior Accountant"),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Accounting / tax."),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting and tax work."),
            Clause(id="C-002", type="out_of_scope", text="Marketing copy."),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="test",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(charter, private)
    return charter


def _failed_verdict() -> Verdict:
    return Verdict(
        decision="incompatible",
        matched_clauses=[
            MatchedClause(
                id="C-002",
                local_decision="incompatible",
                applied=True,
                confidence=0.94,
                reason="Marketing copy.",
            )
        ],
        reason="C-002.",
        rewrite_available=True,
    )


def _good_proposal(text: str = "Classify Q1 invoices.") -> RewriteProposal:
    return RewriteProposal(
        rewritten_task=text,
        why_in_scope="Fits C-001 accounting scope.",
        referenced_clauses=["C-001"],
        remaining_approval_needed=False,
    )


def _allow_hits() -> list[dict[str, Any]]:
    return [
        {
            "id": "C-001",
            "hit": True,
            "confidence": 0.9,
            "reason": "Accounting task.",
        }
    ]


def _block_hits() -> list[dict[str, Any]]:
    return [
        {
            "id": "C-002",
            "hit": True,
            "confidence": 0.9,
            "reason": "Still marketing.",
        }
    ]


@pytest.fixture
def fake_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback checks ANTHROPIC_API_KEY before doing anything else."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch, fake_anthropic_key: None):
    """Return a (set_proposals, set_grader) pair that drives the loop.

    `set_proposals([p1, p2, p3])` defines what propose_within_scope_llm
    returns on attempts 1, 2, 3.

    `set_grader([h1, h2, h3])` defines what _grade_via_llm returns on
    each call. Each entry is a list[hit].
    """
    proposals: list[Any] = []
    graders: list[list[dict[str, Any]]] = []
    call_log: dict[str, list[Any]] = {"propose_calls": [], "grade_calls": []}

    def fake_propose(charter, intended_task, failed_verdict, *, temperature, extra_user_context):  # noqa: ARG001
        idx = len(call_log["propose_calls"])
        call_log["propose_calls"].append(
            {"temperature": temperature, "context": extra_user_context}
        )
        if idx >= len(proposals):
            return None
        return proposals[idx]

    def fake_grade(charter, intended_task):  # noqa: ARG001
        idx = len(call_log["grade_calls"])
        call_log["grade_calls"].append(intended_task)
        if idx >= len(graders):
            return []
        return graders[idx]

    monkeypatch.setattr(loopback_module, "propose_within_scope_llm", fake_propose)
    monkeypatch.setattr(loopback_module, "_grade_via_llm", fake_grade)

    def set_proposals(p: list[Any]) -> None:
        proposals.clear()
        proposals.extend(p)

    def set_graders(g: list[list[dict[str, Any]]]) -> None:
        graders.clear()
        graders.extend(g)

    return set_proposals, set_graders, call_log


# ---------------------------------------------------------------------------
# propose_within_scope_verified — happy path + retry behaviors
# ---------------------------------------------------------------------------


def test_succeeds_on_first_attempt(stub_llm):
    set_proposals, set_graders, log = stub_llm
    set_proposals([_good_proposal()])
    set_graders([_allow_hits()])

    result = propose_within_scope_verified(
        _signed_charter(), "Write marketing copy.", _failed_verdict()
    )

    assert isinstance(result, RewriteProposal)
    assert result.rewritten_task == "Classify Q1 invoices."
    assert len(log["propose_calls"]) == 1
    assert len(log["grade_calls"]) == 1


def test_succeeds_on_second_attempt(stub_llm):
    set_proposals, set_graders, log = stub_llm
    set_proposals([_good_proposal("attempt-1 task"), _good_proposal("attempt-2 task")])
    # First grade hits C-002 (still bad), second grade is clean.
    set_graders([_block_hits(), _allow_hits()])

    result = propose_within_scope_verified(_signed_charter(), "Write marketing.", _failed_verdict())

    assert isinstance(result, RewriteProposal)
    assert result.rewritten_task == "attempt-2 task"
    assert len(log["propose_calls"]) == 2
    # Second propose call must have received feedback context.
    assert log["propose_calls"][1]["context"] is not None
    assert "C-002" in log["propose_calls"][1]["context"]


def test_anneals_temperature(stub_llm):
    set_proposals, set_graders, log = stub_llm
    set_proposals([_good_proposal(), _good_proposal(), _good_proposal()])
    set_graders([_block_hits(), _block_hits(), _allow_hits()])

    propose_within_scope_verified(_signed_charter(), "x", _failed_verdict(), max_attempts=3)

    temps = [c["temperature"] for c in log["propose_calls"]]
    assert temps == [0.2, 0.5, 0.8]


def test_returns_failure_after_max_attempts(stub_llm):
    set_proposals, set_graders, _ = stub_llm
    set_proposals([_good_proposal()] * 3)
    set_graders([_block_hits()] * 3)

    result = propose_within_scope_verified(
        _signed_charter(), "x", _failed_verdict(), max_attempts=3
    )

    assert isinstance(result, RewriteFailure)
    assert len(result.attempts) == 3
    assert all(isinstance(a, RewriteAttempt) for a in result.attempts)
    assert all(
        a.verdict is not None and a.verdict.decision == "incompatible" for a in result.attempts
    )
    assert "Exhausted 3 attempts" in result.reason


def test_handles_proposal_none_in_middle_of_loop(stub_llm):
    set_proposals, set_graders, _ = stub_llm
    # Attempt 1: null. Attempt 2: good rewrite, graded clean.
    set_proposals([None, _good_proposal()])
    set_graders([_allow_hits()])  # only attempt 2 makes a grade call

    result = propose_within_scope_verified(
        _signed_charter(), "x", _failed_verdict(), max_attempts=2
    )

    assert isinstance(result, RewriteProposal)


def test_records_null_proposal_in_history(stub_llm):
    set_proposals, set_graders, _ = stub_llm
    set_proposals([None, None, None])
    set_graders([])

    result = propose_within_scope_verified(
        _signed_charter(), "x", _failed_verdict(), max_attempts=3
    )

    assert isinstance(result, RewriteFailure)
    assert all(a.proposal is None for a in result.attempts)
    assert all(a.verdict is None for a in result.attempts)
    assert all(a.failure_reason is not None for a in result.attempts)


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError, match="max_attempts"):
        propose_within_scope_verified(_signed_charter(), "x", _failed_verdict(), max_attempts=0)


def test_raises_without_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The first thing the loop does is call propose_within_scope_llm, which
    # checks the key. So we expect a RuntimeError from the FIRST attempt.
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        propose_within_scope_verified(_signed_charter(), "x", _failed_verdict())


# ---------------------------------------------------------------------------
# MCP wrapper tests — propose_within_scope_verified
# ---------------------------------------------------------------------------


def _call_tool(tool, *args: Any, **kwargs: Any) -> Any:
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, charter: Charter) -> None:
    monkeypatch.setattr("charter.mcp_server._fetch_and_verify", lambda _url: charter)


def test_mcp_wrapper_returns_ok_on_success(stub_llm, monkeypatch: pytest.MonkeyPatch):
    set_proposals, set_graders, _ = stub_llm
    _stub_fetch(monkeypatch, _signed_charter())
    set_proposals([_good_proposal()])
    set_graders([_allow_hits()])

    result = _call_tool(
        _verified_tool,
        "http://test/x/y",
        "Write marketing copy.",
        _failed_verdict().model_dump(mode="json"),
    )
    assert result["ok"] is True
    assert result["proposal"]["rewritten_task"] == "Classify Q1 invoices."


def test_mcp_wrapper_returns_history_on_failure(stub_llm, monkeypatch: pytest.MonkeyPatch):
    set_proposals, set_graders, _ = stub_llm
    _stub_fetch(monkeypatch, _signed_charter())
    set_proposals([_good_proposal()] * 3)
    set_graders([_block_hits()] * 3)

    result = _call_tool(
        _verified_tool,
        "http://test/x/y",
        "x",
        _failed_verdict().model_dump(mode="json"),
    )
    assert result["ok"] is False
    assert len(result["history"]) == 3
    assert "Exhausted 3 attempts" in result["reason"]


def test_mcp_wrapper_returns_no_llm_on_missing_key(monkeypatch: pytest.MonkeyPatch):
    _stub_fetch(monkeypatch, _signed_charter())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = _call_tool(
        _verified_tool,
        "http://test/x/y",
        "x",
        _failed_verdict().model_dump(mode="json"),
    )
    assert result["ok"] is False
    assert "ANTHROPIC_API_KEY" in result["reason"]


def test_mcp_wrapper_rejects_invalid_verdict(monkeypatch: pytest.MonkeyPatch):
    _stub_fetch(monkeypatch, _signed_charter())
    result = _call_tool(
        _verified_tool,
        "http://test/x/y",
        "x",
        {"not": "a verdict"},
    )
    assert result["ok"] is False
    assert "Verdict" in result["reason"]
