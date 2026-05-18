"""Smoke tests for Charter v0 — verify the parts that don't need an API key.

Run with:
    pytest tests/test_smoke.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from charter.constants import TYPE_TO_DECISION, aggregate_decision
from charter.mcp_server import aggregate_verdict as _agg_tool
from charter.mcp_server import check_inbox as _check_inbox_tool
from charter.mcp_server import delegate_task as _delegate_tool
from charter.mcp_server import read_outbox as _read_outbox_tool
from charter.mcp_server import send_result as _send_result_tool
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
from charter.signing import (
    generate_keypair,
    public_key_to_string,
    sign_charter,
    verify_charter,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_type_to_decision_covers_all_types():
    expected = {
        "scope": "allow",
        "out_of_scope": "incompatible",
        "approval_required": "needs_approval",
        "operational_limit": "needs_approval",
        "style": "allow",
        "data_handling": "needs_approval",
    }
    assert expected == TYPE_TO_DECISION


def test_aggregate_decision_precedence():
    assert aggregate_decision(["allow"]) == "allow"
    assert aggregate_decision(["allow", "needs_approval"]) == "needs_approval"
    assert aggregate_decision(["allow", "needs_approval", "incompatible"]) == "incompatible"
    assert aggregate_decision(["needs_approval", "needs_approval"]) == "needs_approval"
    assert aggregate_decision([]) == "needs_approval"  # zero-match fallback


# ---------------------------------------------------------------------------
# Sign / verify round-trip
# ---------------------------------------------------------------------------


def _make_minimal_charter(public_key_str: str) -> Charter:
    now = datetime.now(UTC).replace(microsecond=0)
    return Charter(
        charter_id="charter:test@example.com:agent_x:2026-05-17",
        binding=Binding(principal_id="test@example.com", agent_id="agent_x"),
        principal=Principal(id="test@example.com", role_summary="Test"),
        issuer=Issuer(id="test@example.com"),
        agent_operator=AgentOperator(id="test_operator"),
        summary=Summary(plain_language="A test Charter."),
        clauses=[
            Clause(id="C-001", type="scope", text="Test scope."),
            Clause(id="C-002", type="out_of_scope", text="Test exclusion."),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_str,
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


def test_sign_and_verify_roundtrip():
    private, public = generate_keypair()
    public_str = public_key_to_string(public)

    charter = _make_minimal_charter(public_str)
    assert charter.provenance.issuer_signature == ""

    sign_charter(charter, private)
    assert charter.provenance.issuer_signature.startswith("ed25519:")
    assert verify_charter(charter) is True


def test_verify_rejects_tampered_charter():
    private, public = generate_keypair()
    public_str = public_key_to_string(public)

    charter = _make_minimal_charter(public_str)
    sign_charter(charter, private)
    assert verify_charter(charter) is True

    # Tamper a clause text — signature should no longer verify.
    charter.clauses[0].text = "Tampered content."
    assert verify_charter(charter) is False


# ---------------------------------------------------------------------------
# aggregate_verdict (MCP protocol layer, no LLM)
# ---------------------------------------------------------------------------

# Note: we import the FastMCP-decorated function under its real name from
# the top of the file. We invoke the underlying callable directly because
# the @mcp.tool() decorator expects an active MCP server context.


def _alice_like_charter() -> dict:
    """A minimal Charter dict (just the bits aggregate_verdict reads)."""
    return {
        "clauses": [
            {"id": "C-001", "type": "scope", "text": "..."},
            {"id": "C-002", "type": "out_of_scope", "text": "..."},
            {"id": "C-003", "type": "approval_required", "text": "..."},
            {"id": "C-004", "type": "approval_required", "text": "..."},
            {"id": "C-005", "type": "operational_limit", "text": "..."},
            {"id": "C-006", "type": "data_handling", "text": "..."},
            {"id": "C-007", "type": "style", "text": "..."},
        ],
    }


def _aggregate(charter: dict, hits: list[dict]) -> dict:
    """Call the FastMCP-decorated function under its real name."""
    # FastMCP @mcp.tool() decorator typically wraps the function. We invoke
    # the underlying callable directly. Different fastmcp versions expose it
    # differently — try a few.
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(_agg_tool, attr):
            return getattr(_agg_tool, attr)(charter, hits)
    return _agg_tool(charter, hits)  # plain callable fallback


def test_aggregate_verdict_incompatible_when_out_of_scope_hit():
    charter = _alice_like_charter()
    hits = [
        {"id": "C-002", "hit": True, "confidence": 0.94, "reason": "code authoring excluded"},
    ]
    v = _aggregate(charter, hits)
    assert v["decision"] == "incompatible"
    assert len(v["matched_clauses"]) == 1
    assert v["matched_clauses"][0]["id"] == "C-002"
    assert v["matched_clauses"][0]["applied"] is True
    assert v["rewrite_available"] is True


def test_aggregate_verdict_needs_approval_overrides_allow():
    """C-001 allow + C-005 needs_approval -> needs_approval wins."""
    charter = _alice_like_charter()
    hits = [
        {"id": "C-001", "hit": True, "confidence": 0.85, "reason": "accounting task"},
        {"id": "C-005", "hit": True, "confidence": 0.80, "reason": "outside hours"},
    ]
    v = _aggregate(charter, hits)
    assert v["decision"] == "needs_approval"
    # C-005 is applied (its local decision matches the aggregate)
    applied = [m for m in v["matched_clauses"] if m["applied"]]
    applied_ids = {m["id"] for m in applied}
    assert "C-005" in applied_ids
    assert "C-001" not in applied_ids  # local "allow" not applied


def test_aggregate_verdict_incompatible_beats_needs_approval():
    """C-002 incompatible + C-003 needs_approval -> incompatible wins."""
    charter = _alice_like_charter()
    hits = [
        {"id": "C-002", "hit": True, "confidence": 0.9, "reason": "marketing"},
        {"id": "C-003", "hit": True, "confidence": 0.8, "reason": "touches PII"},
    ]
    v = _aggregate(charter, hits)
    assert v["decision"] == "incompatible"


def test_aggregate_verdict_allow_when_only_scope_hit():
    charter = _alice_like_charter()
    hits = [
        {"id": "C-001", "hit": True, "confidence": 0.92, "reason": "fits scope"},
    ]
    v = _aggregate(charter, hits)
    assert v["decision"] == "allow"
    assert v["matched_clauses"][0]["applied"] is True
    assert v["rewrite_available"] is False  # only relevant for incompatible


def test_aggregate_verdict_zero_match_defaults_needs_approval():
    charter = _alice_like_charter()
    v = _aggregate(charter, [])
    assert v["decision"] == "needs_approval"
    assert v["matched_clauses"] == []
    assert "No clauses matched" in v["reason"]


def test_aggregate_verdict_low_confidence_fallback():
    """All hits with confidence < 0.5 -> downgrade to needs_approval."""
    charter = _alice_like_charter()
    hits = [
        {"id": "C-002", "hit": True, "confidence": 0.3, "reason": "maybe marketing?"},
    ]
    v = _aggregate(charter, hits)
    assert v["decision"] == "needs_approval"
    assert v["matched_clauses"][0]["applied"] is True
    assert "low confidence" in v["reason"].lower()


def test_aggregate_verdict_ignores_misses():
    """hit=False entries should not pollute the verdict."""
    charter = _alice_like_charter()
    hits = [
        {"id": "C-001", "hit": False, "confidence": 0.1, "reason": "not accounting"},
        {"id": "C-002", "hit": True, "confidence": 0.94, "reason": "marketing"},
    ]
    v = _aggregate(charter, hits)
    assert v["decision"] == "incompatible"
    assert {m["id"] for m in v["matched_clauses"]} == {"C-002"}


# ---------------------------------------------------------------------------
# Inbox / outbox round-trip (delegate_task -> check_inbox -> send_result -> read_outbox)
# ---------------------------------------------------------------------------


def _call(tool, *args, **kwargs):
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Point CHARTER_DATA_DIR at a temp dir so we don't touch real data/."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    yield tmp_path


def test_message_roundtrip(temp_data_dir):
    # 1. Calling agent delegates a task
    out = _call(
        _delegate_tool,
        target_principal_id="alice@acme.com",
        target_agent_id="research_agent_v1",
        intended_task="write a React component",
    )
    assert out["ok"] is True
    task_id = out["task_id"]
    assert (temp_data_dir / "messages" / "inbox.json").exists()

    # 2. Target agent reads the inbox
    inbox = _call(_check_inbox_tool)
    assert inbox is not None
    assert inbox["task_id"] == task_id
    assert inbox["intended_task"] == "write a React component"
    assert inbox["charter_url"].endswith("/alice@acme.com/research_agent_v1")
    assert inbox["status"] == "pending"

    # 3. Target agent replies
    fake_verdict = {
        "decision": "incompatible",
        "matched_clauses": [
            {
                "id": "C-002",
                "local_decision": "incompatible",
                "applied": True,
                "confidence": 0.94,
                "reason": "code authoring excluded",
            }
        ],
        "reason": "test",
        "rewrite_available": True,
    }
    send_out = _call(
        _send_result_tool,
        task_id=task_id,
        verdict=fake_verdict,
        response_text="Declined: applied C-002.",
    )
    assert send_out["ok"] is True

    # 4. Calling agent reads the outbox
    reply = _call(_read_outbox_tool)
    assert reply is not None
    assert reply["task_id"] == task_id
    assert reply["verdict"]["decision"] == "incompatible"
    assert reply["response_text"].startswith("Declined")


def test_check_inbox_empty(temp_data_dir):
    assert _call(_check_inbox_tool) is None


def test_read_outbox_empty(temp_data_dir):
    assert _call(_read_outbox_tool) is None
