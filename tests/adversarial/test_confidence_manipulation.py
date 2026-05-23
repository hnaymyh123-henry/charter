"""Adversarial: confidence-threshold manipulation.

Attack model: an upstream agent (or a prompt-injection payload acting
through the grading LLM) coaxes the grader into reporting confidence
values that hover just below the protocol's `LOW_CONFIDENCE_THRESHOLD`
of 0.5. The hope is that this causes a real out_of_scope hit to be
treated as "weak signal" and downgraded, possibly to `allow`.

Defense (ADR-005): the protocol layer's low-confidence fallback is
deliberately one-directional — when ALL hits are below threshold the
verdict is `needs_approval` (closed-world), never `allow`. The threshold
acts as a brake against silent auto-approval, not as a degrade path
from `incompatible` to `allow`.

These tests assert that property directly against `aggregate_verdict`
and `aggregate_verdict_chain`.
"""

from __future__ import annotations

from typing import Any

from charter.mcp_server import (
    aggregate_verdict as _aggregate_tool,
)
from charter.mcp_server import (
    aggregate_verdict_chain as _chain_agg_tool,
)
from charter.schema import Clause

from .conftest import call_mcp_tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JUST_BELOW = 0.49
_JUST_AT = 0.5
_HIGH = 0.95


def _two_clause_charter(charter_factory: Any) -> Any:
    return charter_factory(
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting."),
            Clause(id="C-002", type="out_of_scope", text="Marketing copy."),
        ],
    )


# ---------------------------------------------------------------------------
# Single-Charter cases
# ---------------------------------------------------------------------------


def test_all_hits_below_threshold_fall_back_to_needs_approval(
    charter_factory: Any,
) -> None:
    """The attack: every hit is reported at 0.49 confidence to dodge the
    threshold. Even though the strongest signal is on C-002 (out_of_scope),
    the protocol degrades to `needs_approval` — NOT `allow`."""
    charter = _two_clause_charter(charter_factory)
    hits = [
        {
            "id": "C-002",
            "hit": True,
            "confidence": _JUST_BELOW,
            "reason": "Maybe marketing-ish.",
        }
    ]
    result = call_mcp_tool(_aggregate_tool, charter.model_dump(mode="json"), hits)
    assert result["decision"] == "needs_approval"
    # The applied flag is set so the operator can see which clause was
    # the (low-confidence) trigger.
    assert any(m["id"] == "C-002" and m["applied"] for m in result["matched_clauses"])


def test_one_high_confidence_among_low_still_triggers_normal_aggregation(
    charter_factory: Any,
) -> None:
    """If at least one hit is above threshold, the low-confidence
    fallback does NOT engage — normal aggregation runs and out_of_scope
    wins. The attack of flooding with low-confidence hits cannot suppress
    a single high-confidence one."""
    charter = _two_clause_charter(charter_factory)
    hits = [
        {
            "id": "C-001",
            "hit": True,
            "confidence": _JUST_BELOW,
            "reason": "Weak scope signal.",
        },
        {
            "id": "C-002",
            "hit": True,
            "confidence": _HIGH,
            "reason": "Strong marketing-copy hit.",
        },
    ]
    result = call_mcp_tool(_aggregate_tool, charter.model_dump(mode="json"), hits)
    assert result["decision"] == "incompatible"


def test_zero_match_fallback_to_needs_approval(charter_factory: Any) -> None:
    """No hits at all (e.g. grader was suppressed entirely) — closed-world
    fallback says `needs_approval`. This is the property that prevents a
    suppressed grader from being treated as 'no objections, allow'."""
    charter = _two_clause_charter(charter_factory)
    result = call_mcp_tool(_aggregate_tool, charter.model_dump(mode="json"), [])
    assert result["decision"] == "needs_approval"


def test_hit_at_exactly_threshold_does_not_fall_back(
    charter_factory: Any,
) -> None:
    """Boundary test: confidence == 0.5 is NOT < 0.5, so a hit exactly at
    the threshold goes through the normal aggregation. This documents the
    exact semantics so an adversary can't pick a value that 'looks
    ambiguous' and ride the edge case."""
    charter = _two_clause_charter(charter_factory)
    hits = [
        {
            "id": "C-002",
            "hit": True,
            "confidence": _JUST_AT,
            "reason": "Boundary value.",
        }
    ]
    result = call_mcp_tool(_aggregate_tool, charter.model_dump(mode="json"), hits)
    assert result["decision"] == "incompatible"


# ---------------------------------------------------------------------------
# Chain cases
# ---------------------------------------------------------------------------


def test_chain_all_hits_below_threshold_fall_back_to_needs_approval(
    charter_factory: Any,
) -> None:
    """Same property across a chain: low-confidence flood across multiple
    Charters still degrades to `needs_approval`, never `allow`."""
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[Clause(id="P-002", type="out_of_scope", text="Marketing.")],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[Clause(id="C-002", type="out_of_scope", text="Marketing.")],
    )
    chain = [parent.model_dump(mode="json"), child.model_dump(mode="json")]
    hits = {
        "parent:1": [
            {
                "id": "P-002",
                "hit": True,
                "confidence": _JUST_BELOW,
                "reason": "weak",
            }
        ],
        "child:1": [
            {
                "id": "C-002",
                "hit": True,
                "confidence": _JUST_BELOW,
                "reason": "weak",
            }
        ],
    }
    result = call_mcp_tool(_chain_agg_tool, chain, hits)
    assert result["decision"] == "needs_approval"
