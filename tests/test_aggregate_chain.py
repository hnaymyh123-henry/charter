"""Tests for the `aggregate_verdict_chain` MCP tool."""

from __future__ import annotations

from typing import Any

from charter.mcp_server import aggregate_verdict_chain as _tool


def _call(tool: Any, *args: Any, **kwargs: Any) -> Any:
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


# Minimal Charter dicts — we only need charter_id + clauses[].
def _charter(cid: str, clause_specs: list[tuple[str, str]]) -> dict[str, Any]:
    """clause_specs: list of (clause_id, type)."""
    return {
        "charter_id": cid,
        "clauses": [{"id": cid_, "type": t, "text": "..."} for cid_, t in clause_specs],
    }


# ---------------------------------------------------------------------------
# Empty / zero-match fallbacks
# ---------------------------------------------------------------------------


def test_empty_chain_returns_error() -> None:
    result = _call(_tool, [], {})
    assert result.get("ok") is False
    assert "empty chain" in result["reason"]


def test_two_hop_chain_with_no_hits_anywhere_defaults_to_needs_approval() -> None:
    chain = [
        _charter("parent:1", [("C-001", "scope"), ("C-002", "out_of_scope")]),
        _charter("child:1", [("C-001", "scope"), ("C-002", "out_of_scope")]),
    ]
    result = _call(_tool, chain, {})
    assert result["decision"] == "needs_approval"
    assert result["matched_clauses"] == []
    assert "No clauses matched anywhere in the chain" in result["reason"]


# ---------------------------------------------------------------------------
# Single-Charter behavior surfaced via the chain tool
# ---------------------------------------------------------------------------


def test_single_charter_chain_behaves_like_single_aggregate() -> None:
    """A one-element chain should match the single-Charter result shape."""
    chain = [_charter("parent:1", [("C-001", "scope"), ("C-002", "out_of_scope")])]
    hits = {
        "parent:1": [
            {"id": "C-002", "hit": True, "confidence": 0.9, "reason": "marketing"},
        ]
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "incompatible"
    assert len(result["matched_clauses"]) == 1
    assert result["matched_clauses"][0]["id"] == "C-002"
    assert result["matched_clauses"][0]["applied"] is True
    assert result["matched_clauses"][0]["source_charter_id"] == "parent:1"
    assert result["rewrite_available"] is True


# ---------------------------------------------------------------------------
# Cross-Charter aggregation — strictest wins
# ---------------------------------------------------------------------------


def test_parent_incompatible_wins_over_child_allow() -> None:
    """Parent's out_of_scope hit should force `incompatible` even if the
    child's scope allows the task — strictest in the chain wins."""
    chain = [
        _charter("parent:1", [("C-001", "scope"), ("C-002", "out_of_scope")]),
        _charter("child:1", [("C-001", "scope")]),
    ]
    hits = {
        "parent:1": [
            {"id": "C-002", "hit": True, "confidence": 0.95, "reason": "marketing"},
        ],
        "child:1": [
            {"id": "C-001", "hit": True, "confidence": 0.9, "reason": "in scope"},
        ],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "incompatible"
    applied = [m for m in result["matched_clauses"] if m["applied"]]
    assert len(applied) == 1
    assert applied[0]["source_charter_id"] == "parent:1"
    assert applied[0]["id"] == "C-002"
    # Child's scope clause is matched but not applied (allow != incompatible).
    not_applied = [m for m in result["matched_clauses"] if not m["applied"]]
    assert any(m["source_charter_id"] == "child:1" for m in not_applied)


def test_child_incompatible_wins_over_parent_allow() -> None:
    """Child adding a new out_of_scope clause the parent doesn't have
    should still force incompatible at the chain level."""
    chain = [
        _charter("parent:1", [("C-001", "scope")]),
        _charter("child:1", [("C-001", "scope"), ("C-002", "out_of_scope")]),
    ]
    hits = {
        "parent:1": [
            {"id": "C-001", "hit": True, "confidence": 0.9, "reason": "in scope"},
        ],
        "child:1": [
            {"id": "C-002", "hit": True, "confidence": 0.9, "reason": "blocked"},
        ],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "incompatible"
    applied = [m for m in result["matched_clauses"] if m["applied"]]
    assert applied[0]["source_charter_id"] == "child:1"


def test_needs_approval_overrides_allow_across_chain() -> None:
    chain = [
        _charter("parent:1", [("C-001", "scope")]),
        _charter("child:1", [("C-001", "scope"), ("C-003", "approval_required")]),
    ]
    hits = {
        "parent:1": [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "scope"}],
        "child:1": [{"id": "C-003", "hit": True, "confidence": 0.85, "reason": "PII"}],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "needs_approval"


def test_incompatible_beats_needs_approval_across_chain() -> None:
    chain = [
        _charter("root:1", [("C-002", "out_of_scope")]),
        _charter("mid:1", [("C-003", "approval_required")]),
        _charter("leaf:1", [("C-001", "scope")]),
    ]
    hits = {
        "root:1": [{"id": "C-002", "hit": True, "confidence": 0.9, "reason": "blocked"}],
        "mid:1": [{"id": "C-003", "hit": True, "confidence": 0.85, "reason": "needs ok"}],
        "leaf:1": [{"id": "C-001", "hit": True, "confidence": 0.95, "reason": "scope"}],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "incompatible"
    applied = [m for m in result["matched_clauses"] if m["applied"]]
    # Only the incompatible clause is applied; needs_approval matched but
    # not applied to the final decision.
    assert len(applied) == 1
    assert applied[0]["source_charter_id"] == "root:1"


# ---------------------------------------------------------------------------
# rewrite_available flag
# ---------------------------------------------------------------------------


def test_rewrite_available_when_any_charter_out_of_scope_hit() -> None:
    chain = [
        _charter("parent:1", [("C-001", "scope")]),
        _charter("child:1", [("C-002", "out_of_scope")]),
    ]
    hits = {
        "child:1": [{"id": "C-002", "hit": True, "confidence": 0.9, "reason": "blocked"}],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "incompatible"
    assert result["rewrite_available"] is True


def test_rewrite_unavailable_when_only_approval_required_hit() -> None:
    chain = [
        _charter("parent:1", [("C-001", "scope")]),
        _charter("child:1", [("C-003", "approval_required")]),
    ]
    hits = {
        "child:1": [{"id": "C-003", "hit": True, "confidence": 0.9, "reason": "PII"}],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "needs_approval"
    assert result["rewrite_available"] is False


# ---------------------------------------------------------------------------
# Reason string includes source_charter_id::clause_id refs
# ---------------------------------------------------------------------------


def test_reason_names_applied_clauses_with_source_qualifier() -> None:
    chain = [
        _charter("parent:1", [("C-002", "out_of_scope")]),
        _charter("child:1", [("C-001", "scope")]),
    ]
    hits = {
        "parent:1": [{"id": "C-002", "hit": True, "confidence": 0.9, "reason": "x"}],
    }
    result = _call(_tool, chain, hits)
    assert "parent:1::C-002" in result["reason"]


# ---------------------------------------------------------------------------
# Edge case: misses are ignored
# ---------------------------------------------------------------------------


def test_misses_do_not_pollute_matched_clauses() -> None:
    chain = [_charter("p:1", [("C-001", "scope"), ("C-002", "out_of_scope")])]
    hits = {
        "p:1": [
            {"id": "C-001", "hit": False, "confidence": 0.1, "reason": "not scope"},
            {"id": "C-002", "hit": True, "confidence": 0.9, "reason": "blocked"},
        ]
    }
    result = _call(_tool, chain, hits)
    assert {m["id"] for m in result["matched_clauses"]} == {"C-002"}


# ---------------------------------------------------------------------------
# Edge case: low-confidence fallback applies to whole-chain confidence
# ---------------------------------------------------------------------------


def test_low_confidence_across_chain_downgrades_to_needs_approval() -> None:
    chain = [_charter("p:1", [("C-002", "out_of_scope")])]
    hits = {
        "p:1": [{"id": "C-002", "hit": True, "confidence": 0.3, "reason": "maybe"}],
    }
    result = _call(_tool, chain, hits)
    assert result["decision"] == "needs_approval"
    assert "low confidence" in result["reason"].lower()
