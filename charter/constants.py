"""Protocol constants for Charter v0.

These values are part of the protocol contract — do not change at runtime.
"""

from typing import Literal

Decision = Literal["allow", "needs_approval", "incompatible"]

ClauseType = Literal[
    "scope",
    "out_of_scope",
    "approval_required",
    "operational_limit",
    "style",
    "data_handling",
]


# Local-decision mapping per clause.type (decision §P1-4 ②).
# A clause's type determines the per-clause "local decision" deterministically;
# the LLM only decides whether a clause is hit by the intended task.
TYPE_TO_DECISION: dict[str, Decision] = {
    "scope": "allow",
    "out_of_scope": "incompatible",
    "approval_required": "needs_approval",
    "operational_limit": "needs_approval",
    "style": "allow",
    "data_handling": "needs_approval",
}


# Aggregate-decision precedence (§P2-11 ⑫).
# `incompatible` always beats `needs_approval` which always beats `allow`.
_DECISION_RANK: dict[str, int] = {
    "allow": 0,
    "needs_approval": 1,
    "incompatible": 2,
}


CHARTER_VERSION = "0.1"
DEFAULT_VALID_DAYS = 30
LOW_CONFIDENCE_THRESHOLD = 0.5  # §P2-11 ⑭

DEFAULT_URL_BASE = "http://localhost:8000"
DEFAULT_DATA_DIR = "./data"
DEFAULT_MODEL = "claude-sonnet-4-6"


def aggregate_decision(local_decisions: list[Decision]) -> Decision:
    """Combine per-clause local decisions into a single Aggregate Decision.

    Rule: `incompatible` > `needs_approval` > `allow`. If the list is empty,
    return `needs_approval` as the conservative zero-match fallback.
    """
    if not local_decisions:
        return "needs_approval"
    return max(local_decisions, key=lambda d: _DECISION_RANK[d])
