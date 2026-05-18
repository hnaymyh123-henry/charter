"""Tests for `charter.adapters.openai_agents`.

These tests do NOT require `openai-agents` to be installed; the
adapter's preflight and decorator are pure-Python wrappers around
`charter`'s own MCP tool surface, which we already stub elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from charter.adapters.openai_agents import charter_gated, charter_preflight
from charter.schema import (
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    Provenance,
    SourceCommitment,
    Summary,
)
from charter.signing import generate_keypair, public_key_to_string, sign_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _signed_charter() -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    c = Charter(
        charter_id="charter:alice@acme.com:research_agent_v1:2026-05-18",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Test"),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting"),
            Clause(id="C-002", type="out_of_scope", text="Code authoring"),
            Clause(
                id="C-003",
                type="approval_required",
                text="PII handling",
            ),
        ],
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
    )
    sign_charter(c, private)
    return c


@pytest.fixture
def stub_fetch(monkeypatch: pytest.MonkeyPatch):
    charter = _signed_charter()
    monkeypatch.setattr(
        "charter.adapters.openai_agents._fetch_and_verify",
        lambda _url: charter,
    )
    return charter


def _grader_returning(hits: list[dict[str, Any]]):
    """Return a hits_grader stub that always returns the given hits."""

    def grader(_charter: Charter, _task: str) -> list[dict[str, Any]]:
        return list(hits)

    return grader


# ---------------------------------------------------------------------------
# charter_preflight
# ---------------------------------------------------------------------------


def test_preflight_returns_allow_for_in_scope_task(stub_fetch):
    grader = _grader_returning(
        [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "accounting"}]
    )
    v = charter_preflight("http://test/x", "Reconcile invoices.", hits_grader=grader)
    assert v.decision == "allow"


def test_preflight_returns_incompatible_for_out_of_scope_task(stub_fetch):
    grader = _grader_returning(
        [{"id": "C-002", "hit": True, "confidence": 0.95, "reason": "writes code"}]
    )
    v = charter_preflight("http://test/x", "Write React.", hits_grader=grader)
    assert v.decision == "incompatible"


def test_preflight_returns_needs_approval(stub_fetch):
    grader = _grader_returning([{"id": "C-003", "hit": True, "confidence": 0.85, "reason": "PII"}])
    v = charter_preflight("http://test/x", "Look at SSN data.", hits_grader=grader)
    assert v.decision == "needs_approval"


def test_preflight_default_grader_is_loopback_grader(stub_fetch, monkeypatch):
    """When hits_grader is omitted, _default_grader() returns
    charter.loopback._grade_via_llm."""
    called = {"count": 0}

    def stub_grader(_charter, _task):
        called["count"] += 1
        return []

    # Patch where the adapter looks it up.
    monkeypatch.setattr("charter.loopback._grade_via_llm", stub_grader)

    charter_preflight("http://test/x", "task")
    assert called["count"] == 1


def test_preflight_emits_log(stub_fetch, caplog):
    import logging

    grader = _grader_returning([])
    with caplog.at_level(logging.INFO, logger="charter.adapters.openai_agents"):
        charter_preflight("http://test/x", "task", hits_grader=grader)

    rec = next(r for r in caplog.records if r.name == "charter.adapters.openai_agents")
    assert rec.decision in {"allow", "needs_approval", "incompatible"}
    assert rec.charter_id == "charter:alice@acme.com:research_agent_v1:2026-05-18"


# ---------------------------------------------------------------------------
# charter_gated decorator
# ---------------------------------------------------------------------------


def test_gated_lets_in_scope_call_through(stub_fetch):
    grader = _grader_returning([{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "ok"}])

    calls = []

    @charter_gated("http://test/x", hits_grader=grader)
    def my_tool(task: str) -> str:
        calls.append(task)
        return f"done: {task}"

    out = my_tool("Reconcile invoices.")
    assert out == "done: Reconcile invoices."
    assert calls == ["Reconcile invoices."]


def test_gated_blocks_incompatible_task(stub_fetch):
    grader = _grader_returning([{"id": "C-002", "hit": True, "confidence": 0.95, "reason": "code"}])

    calls = []

    @charter_gated("http://test/x", hits_grader=grader)
    def my_tool(task: str) -> str:
        calls.append(task)
        return f"done: {task}"

    out = my_tool("Write React.")
    assert calls == []  # body never ran
    assert "incompatible" in out
    assert "C-002" in out


def test_gated_blocks_needs_approval_by_default(stub_fetch):
    grader = _grader_returning([{"id": "C-003", "hit": True, "confidence": 0.85, "reason": "PII"}])

    @charter_gated("http://test/x", hits_grader=grader)
    def my_tool(task: str) -> str:
        return "done"

    out = my_tool("SSN lookup.")
    assert "needs_approval" in out


def test_gated_refuse_on_only_incompatible_lets_needs_approval_through(stub_fetch):
    """Caller can opt approval-required tasks through by narrowing
    refuse_on to {"incompatible"} only."""
    grader = _grader_returning([{"id": "C-003", "hit": True, "confidence": 0.85, "reason": "PII"}])

    @charter_gated("http://test/x", hits_grader=grader, refuse_on=("incompatible",))
    def my_tool(task: str) -> str:
        return f"executed: {task}"

    out = my_tool("SSN lookup.")
    assert out == "executed: SSN lookup."


def test_gated_empty_refuse_on_is_rejected():
    with pytest.raises(ValueError, match="refuse_on"):

        @charter_gated("http://test/x", refuse_on=())
        def _t(task: str) -> str:
            return task


def test_gated_task_from_callable(stub_fetch):
    grader = _grader_returning([{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "ok"}])

    @charter_gated(
        "http://test/x",
        hits_grader=grader,
        task_from=lambda payload, **_: payload["description"],
    )
    def my_tool(payload: dict) -> str:
        return f"done: {payload['description']}"

    out = my_tool({"description": "Reconcile invoices."})
    assert out == "done: Reconcile invoices."


def test_gated_preserves_function_metadata(stub_fetch):
    """@functools.wraps usage means the decorated function keeps its name + doc."""
    grader = _grader_returning([])

    @charter_gated("http://test/x", hits_grader=grader)
    def original(task: str) -> str:
        """Original docstring."""
        return task

    assert original.__name__ == "original"
    assert original.__doc__ == "Original docstring."
