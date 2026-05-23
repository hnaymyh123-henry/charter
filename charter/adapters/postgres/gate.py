"""Charter gate for SQL intents.

Bridges the SQL-side ``SqlIntent`` (see :mod:`.intent`) and the
Charter-side ``aggregate_verdict`` primitive. The bridge is the
*projection*: a deterministic natural-language description of the SQL
operation that the Charter grader can reason about without ever
seeing the raw SQL.

The grader is dependency-injected (``hits_grader``) so the adapter
does not pick an LLM provider for the caller. ADR-009 forbids the
adapter from making implicit LLM calls; the default grader stub here
returns the maximally-restrictive set of clause hits (every clause
hit, confidence 1.0), which collapses to ``incompatible`` whenever
the Charter contains any restrictive clause — i.e. the fail-closed
posture when no grader is configured.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..._logging import get_logger
from ...mcp_server import aggregate_verdict as _aggregate_verdict_tool
from ...schema import Charter, MatchedClause, Verdict
from .intent import SqlIntent

_log = get_logger("charter.adapters.postgres.gate")

HitsGrader = Callable[[Charter, str], list[dict[str, Any]]]


def _call_aggregate_verdict(charter: Charter, hits: list[dict[str, Any]]) -> Verdict:
    """Invoke ``aggregate_verdict`` past the ``@mcp.tool`` wrapper.

    Mirrors the unwrap pattern used by the OpenAI Agents and AP2
    adapters — the MCP tool decorator hides the underlying function
    behind one of ``fn``/``func``/``__wrapped__`` depending on which
    SDK version is installed.
    """
    payload = charter.model_dump(mode="json")
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(_aggregate_verdict_tool, attr):
            raw = getattr(_aggregate_verdict_tool, attr)(payload, hits)
            return Verdict.model_validate(raw)
    raw = _aggregate_verdict_tool(payload, hits)
    return Verdict.model_validate(raw)


def intent_to_task(intent: SqlIntent) -> str:
    """Render a :class:`SqlIntent` into a one-line task description.

    The grader sees this string, not the raw SQL. The phrasing
    deliberately uses operator nouns ("read from", "delete rows in")
    rather than SQL keywords so the LLM does not get distracted by
    syntax. PII tagging adds an explicit hint without quoting any
    particular column name.

    Examples::

        SqlIntent("SELECT", ["public.orders"], False)
          -> "Read rows from Postgres table(s): public.orders."
        SqlIntent("DELETE", ["customers"], True)
          -> "Delete rows from Postgres table(s): customers. "
             "Statement references columns that may contain PII."
        SqlIntent("OTHER", [], True)
          -> "Execute an unclassified SQL statement against Postgres. "
             "Statement references columns that may contain PII."
    """
    verb = {
        "SELECT": "Read rows from",
        "INSERT": "Insert rows into",
        "UPDATE": "Update rows in",
        "DELETE": "Delete rows from",
        "DDL": "Run a DDL (CREATE/DROP/ALTER/TRUNCATE) statement against",
        "OTHER": "Execute an unclassified SQL statement against",
    }.get(intent.operation, "Execute an unclassified SQL statement against")

    if intent.tables:
        head = f"{verb} Postgres table(s): {', '.join(intent.tables)}."
    elif intent.operation == "OTHER":
        head = f"{verb} Postgres."
    else:
        head = f"{verb} Postgres (table not statically determinable)."

    if intent.has_pii_columns:
        head += " Statement references columns that may contain PII."
    return head


def _default_hits_grader(charter: Charter, _task: str) -> list[dict[str, Any]]:
    """Conservative default grader: report every clause as hit.

    Why this default is safe: it forces ``aggregate_verdict`` to apply
    every restrictive clause the Charter carries. A Charter that
    contains *any* ``out_of_scope`` or ``approval_required`` clause
    will therefore refuse / require approval under this grader. A
    Charter with only ``scope`` clauses will allow — which is the
    correct outcome, because a Charter that *positively scopes* the
    operation has already authorized it by construction.

    Production deployments should inject a real grader (LLM-backed)
    that only marks the clauses actually hit by the intent — that
    grader produces sharper decisions but requires an LLM call per
    statement. The proxy lets callers choose.
    """
    hits: list[dict[str, Any]] = []
    for clause in charter.clauses:
        hits.append(
            {
                "id": clause.id,
                "hit": True,
                "confidence": 1.0,
                "reason": "default grader: all clauses treated as hit (fail-closed)",
            }
        )
    return hits


def _incompatible(reason: str) -> Verdict:
    """Build a synthetic ``incompatible`` verdict.

    Used by all of the fail-closed branches in :func:`check` so the
    proxy never produces a verdict the calling client cannot interpret
    — it is always a real :class:`Verdict` carrying a human-readable
    ``reason`` we can echo into the PG ``ErrorResponse``.
    """
    return Verdict(
        decision="incompatible",
        matched_clauses=[
            MatchedClause(
                id="charter-pg-proxy:fail-closed",
                local_decision="incompatible",
                applied=True,
                confidence=1.0,
                reason=reason,
            )
        ],
        reason=reason,
    )


def check(
    intent: SqlIntent,
    charter: Charter,
    hits_grader: HitsGrader | None = None,
) -> Verdict:
    """Run the Charter compatibility check against a SQL intent.

    The flow:

      1. If the intent is the fail-closed sentinel (``OTHER`` and no
         tables), refuse without invoking the grader.
      2. Project the intent into a task string.
      3. Call ``hits_grader(charter, task)``. Any exception
         (including from a misconfigured caller-provided grader)
         collapses into ``incompatible``.
      4. Hand the hits to ``aggregate_verdict`` and return its
         verdict unmodified.

    Args:
        intent:        Output of :func:`intent_from_sql`.
        charter:       Verified :class:`Charter` (already fetched +
                       signature-checked by the proxy).
        hits_grader:   Optional grader override. Default is
                       :func:`_default_hits_grader` — the conservative
                       "all clauses hit" stub described above.

    Returns:
        A :class:`Verdict`. The proxy refuses iff
        ``verdict.decision != "allow"``.
    """
    if intent.operation == "OTHER" and not intent.tables:
        verdict = _incompatible(
            "SQL could not be classified into a known operation against a known table; "
            "refusing under fail-closed policy."
        )
        _log.warning(
            "gate fail-closed: unclassified intent",
            extra={
                "outcome": "incompatible",
                "operation": intent.operation,
                "charter_id": charter.charter_id,
            },
        )
        return verdict

    task = intent_to_task(intent)
    grader = hits_grader or _default_hits_grader
    try:
        hits = grader(charter, task)
    except Exception as e:
        verdict = _incompatible(
            f"Charter grader raised {type(e).__name__}: {e}; refusing under fail-closed policy."
        )
        _log.exception(
            "gate fail-closed: grader raised",
            extra={
                "outcome": "incompatible",
                "operation": intent.operation,
                "tables": intent.tables,
                "charter_id": charter.charter_id,
            },
        )
        return verdict

    try:
        verdict = _call_aggregate_verdict(charter, hits)
    except Exception as e:
        verdict = _incompatible(
            f"aggregate_verdict raised {type(e).__name__}: {e}; refusing under fail-closed policy."
        )
        _log.exception(
            "gate fail-closed: aggregate_verdict raised",
            extra={
                "outcome": "incompatible",
                "operation": intent.operation,
                "tables": intent.tables,
                "charter_id": charter.charter_id,
            },
        )
        return verdict

    _log.info(
        "gate decision",
        extra={
            "outcome": verdict.decision,
            "operation": intent.operation,
            "tables": intent.tables,
            "has_pii": intent.has_pii_columns,
            "charter_id": charter.charter_id,
            "decision": verdict.decision,
        },
    )
    return verdict


__all__ = [
    "HitsGrader",
    "check",
    "intent_to_task",
]
