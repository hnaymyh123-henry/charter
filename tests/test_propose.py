"""Unit tests for `propose_within_scope_llm` + the MCP wrapper.

The LLM is stubbed via monkeypatch on `charter.propose.anthropic.Anthropic`
so tests don't make network calls and don't need an API key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from charter import propose as propose_module
from charter.mcp_server import propose_within_scope as _propose_tool
from charter.propose import propose_within_scope_llm
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
    RewriteProposal,
    SourceCommitment,
    Summary,
    Verdict,
)
from charter.signing import generate_keypair, public_key_to_string, sign_charter

# ---------------------------------------------------------------------------
# Helpers
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
                reason="Task is marketing copy.",
            )
        ],
        reason="C-002 excludes marketing copy.",
        rewrite_available=True,
    )


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]


class _FakeMessages:
    def __init__(self, response_text: str) -> None:
        self._response = response_text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return _FakeMessage(self._response)


class _FakeAnthropic:
    """Stand-in for `anthropic.Anthropic()` that returns a canned response."""

    def __init__(self, response_text: str) -> None:
        self.messages = _FakeMessages(response_text)


@pytest.fixture
def stub_anthropic(monkeypatch: pytest.MonkeyPatch):
    """Replace `anthropic.Anthropic` with a controllable stub.

    Returns a setter the test can call with whatever JSON it wants the
    fake LLM to "respond" with.
    """
    state: dict[str, _FakeAnthropic | None] = {"client": None}

    def set_response(text: str) -> _FakeAnthropic:
        client = _FakeAnthropic(text)
        state["client"] = client
        return client

    def factory(*args: Any, **kwargs: Any) -> _FakeAnthropic:
        assert state["client"] is not None, "call set_response first"
        return state["client"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    monkeypatch.setattr(propose_module.anthropic, "Anthropic", factory)
    return set_response


# ---------------------------------------------------------------------------
# propose_within_scope_llm — direct helper tests
# ---------------------------------------------------------------------------


def test_returns_proposal_on_valid_json(stub_anthropic):
    stub_anthropic(
        '{"rewritten_task": "Classify invoices for Q1 tax filing.", '
        '"why_in_scope": "Fits C-001 accounting scope; avoids C-002.", '
        '"referenced_clauses": ["C-001", "C-002"], '
        '"remaining_approval_needed": false}'
    )
    result = propose_within_scope_llm(
        _signed_charter(), "Write a marketing email.", _failed_verdict()
    )
    assert isinstance(result, RewriteProposal)
    assert result.rewritten_task.startswith("Classify invoices")
    assert "C-001" in result.referenced_clauses
    assert result.remaining_approval_needed is False


def test_returns_none_on_literal_null(stub_anthropic):
    stub_anthropic("null")
    result = propose_within_scope_llm(_signed_charter(), "Write a song.", _failed_verdict())
    assert result is None


def test_returns_none_on_parse_failure(stub_anthropic):
    stub_anthropic("this is not JSON at all")
    result = propose_within_scope_llm(_signed_charter(), "x", _failed_verdict())
    assert result is None


def test_returns_none_on_schema_mismatch(stub_anthropic):
    # Valid JSON but missing required fields.
    stub_anthropic('{"rewritten_task": "x"}')
    result = propose_within_scope_llm(_signed_charter(), "y", _failed_verdict())
    assert result is None


def test_strips_markdown_fences(stub_anthropic):
    stub_anthropic(
        "```json\n"
        '{"rewritten_task": "ok", "why_in_scope": "fits", '
        '"referenced_clauses": ["C-001"], "remaining_approval_needed": false}\n'
        "```"
    )
    result = propose_within_scope_llm(_signed_charter(), "x", _failed_verdict())
    assert isinstance(result, RewriteProposal)
    assert result.rewritten_task == "ok"


def test_raises_runtime_error_without_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        propose_within_scope_llm(_signed_charter(), "x", _failed_verdict())


def test_passes_temperature_to_client(stub_anthropic):
    fake = stub_anthropic(
        '{"rewritten_task": "x", "why_in_scope": "y", '
        '"referenced_clauses": [], "remaining_approval_needed": false}'
    )
    propose_within_scope_llm(_signed_charter(), "task", _failed_verdict(), temperature=0.7)
    assert fake.messages.calls[0]["temperature"] == 0.7


def test_extra_user_context_appended(stub_anthropic):
    fake = stub_anthropic(
        '{"rewritten_task": "x", "why_in_scope": "y", '
        '"referenced_clauses": [], "remaining_approval_needed": false}'
    )
    propose_within_scope_llm(
        _signed_charter(),
        "task",
        _failed_verdict(),
        extra_user_context="Attempt 2 of 3: earlier rewrite still hit C-002.",
    )
    user_msg = fake.messages.calls[0]["messages"][0]["content"]
    assert "Attempt 2 of 3" in user_msg


# ---------------------------------------------------------------------------
# MCP tool wrapper tests — propose_within_scope
# ---------------------------------------------------------------------------


def _call_tool(tool, *args: Any, **kwargs: Any) -> Any:
    """Invoke a FastMCP-decorated function under its real name."""
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, charter: Charter) -> None:
    """Make `_fetch_and_verify` return the given Charter without HTTP."""
    monkeypatch.setattr("charter.mcp_server._fetch_and_verify", lambda _url: charter)


def test_mcp_wrapper_returns_ok_on_successful_proposal(
    monkeypatch: pytest.MonkeyPatch, stub_anthropic
):
    _stub_fetch(monkeypatch, _signed_charter())
    stub_anthropic(
        '{"rewritten_task": "Classify Q1 invoices.", '
        '"why_in_scope": "fits C-001 scope; avoids C-002.", '
        '"referenced_clauses": ["C-001"], "remaining_approval_needed": false}'
    )
    result = _call_tool(
        _propose_tool,
        "http://test/alice@acme.com/research_agent_v1",
        "Write a marketing email.",
        _failed_verdict().model_dump(mode="json"),
    )
    assert result["ok"] is True
    assert result["proposal"]["rewritten_task"] == "Classify Q1 invoices."


def test_mcp_wrapper_returns_no_rewrite_when_llm_returns_null(
    monkeypatch: pytest.MonkeyPatch, stub_anthropic
):
    _stub_fetch(monkeypatch, _signed_charter())
    stub_anthropic("null")
    result = _call_tool(
        _propose_tool,
        "http://test/x/y",
        "Write a song.",
        _failed_verdict().model_dump(mode="json"),
    )
    assert result["ok"] is False
    assert "no viable rewrite" in result["reason"]


def test_mcp_wrapper_returns_no_llm_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_fetch(monkeypatch, _signed_charter())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = _call_tool(
        _propose_tool,
        "http://test/x/y",
        "task",
        _failed_verdict().model_dump(mode="json"),
    )
    assert result["ok"] is False
    assert "ANTHROPIC_API_KEY" in result["reason"]


def test_mcp_wrapper_rejects_invalid_verdict(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_fetch(monkeypatch, _signed_charter())
    result = _call_tool(
        _propose_tool,
        "http://test/x/y",
        "task",
        {"not": "a verdict"},
    )
    assert result["ok"] is False
    assert "Verdict" in result["reason"]
