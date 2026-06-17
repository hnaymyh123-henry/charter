"""Baseline executors for arms A and B of the governance experiment (wp-experiment).

The experiment isolates GOVERNANCE as the single variable. All three arms run
the SAME task batch (injections.TASK_BATCH) and — when live — the SAME Qwen
grader, so any measured delta attributes to the governance layer, not the model
or the data.

    Arm A — single agent, NO governance.
        One generalist executor with no charter, no role separation, no gate.
        It does whatever the (possibly injected) task says. This is the
        track-required single-agent baseline: maximally capable, maximally
        unsafe. Expected: high raw success on clean tasks, ~0 intercept on
        attacks (it happily exfiltrates).

    Arm B — multi agent, NO charter.
        Tasks are routed to role-named workers (so decomposition exists), but
        there is NO signed charter and NO aggregate_verdict gate. Routing may
        even be *correct*, yet nothing stops a worker from executing an
        out-of-scope injected instruction. Expected: better routing than A,
        still ~0 intercept — proving that role-splitting ALONE is not safety.

    Arm C lives in experiment.py (delegates to wp-orchestrator's CFOOrchestrator
    + the stepup gate). Only C should intercept the exfil as `incompatible`.

These executors are DETERMINISTIC stubs so the experiment runs in <60s with no
live LLM (same no-LLM doctrine as scripts/demo_chain.py). When a live arm is
desired, swap `_keyword_router` / `_naive_execute` for a Qwen-backed
HitsGrader-driven path; the CaseOutcome contract stays identical.
"""

from __future__ import annotations

import time

from .injections import resolve_injection
from .metrics import CaseOutcome, TaskCase

# ---------------------------------------------------------------------------
# Shared deterministic helpers
# ---------------------------------------------------------------------------

# Minimal keyword router so arm B can demonstrate "decomposition + role
# assignment" WITHOUT a charter. Intentionally naive: it routes on surface
# keywords and has no notion of clause/scope, so it cannot catch an injection
# that lands on the "right" worker. In the LIVE configuration this is replaced
# by a Qwen reasoning call shared with arm C's router.
_ROUTE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("invoice", "record", "books", "bookkeep"), "bookkeeping_agent_v1"),
    (("tax", "filing", "liability", "drop", "cleanup"), "tax_filing_agent_v1"),
    (("email", "send", "notify", "auditor", "board"), "comms_agent_v1"),
    (("summary", "read-only", "reconcile", "revenue", "ledger"), "data_analyst_agent_v1"),
]


def _keyword_router(task_text: str) -> str | None:
    low = task_text.lower()
    for keywords, agent_id in _ROUTE_KEYWORDS:
        if any(k in low for k in keywords):
            return agent_id
    return None


# A toy "exfil detector" is intentionally NOT present: the whole point of arms
# A/B is that without a signed charter + deterministic aggregate gate, there is
# no principled place to stop the injected instruction. Arm B executes it.


def _naive_execute(task_text: str) -> bool:  # noqa: ARG001
    """Stub: a no-governance worker always 'succeeds' at executing the task.

    Returns True (executed). The danger is precisely that it executes even the
    injected exfil — which is what the intercept_rate delta will expose.
    """
    return True


# ---------------------------------------------------------------------------
# Arm A: single agent, no governance
# ---------------------------------------------------------------------------


def run_arm_a(cases: list[TaskCase]) -> list[CaseOutcome]:
    """One generalist executor, no charter, no gate, no role split.

    routed_to is always the same pseudo-agent ("generalist") so route_accuracy
    measures ~0 for cases with a specific expected owner (A has no routing).
    effective_decision is "allow" for everything (no governance to deny).
    """
    outcomes: list[CaseOutcome] = []
    for c in cases:
        task_text = resolve_injection(c)
        executed = _naive_execute(task_text)
        outcomes.append(
            CaseOutcome(
                case_id=c.id,
                arm="A",
                effective_decision="allow",  # nothing can say otherwise
                routed_to="generalist",
                executed=executed,
                grant_applied=False,
                conflict_ms=None,  # A never pauses; no conflict to resolve
                notes="single-agent no-governance: executes task as given",
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# Arm B: multi agent, no charter
# ---------------------------------------------------------------------------


def run_arm_b(cases: list[TaskCase]) -> list[CaseOutcome]:
    """Role-named workers + a keyword router, but NO signed charter / NO gate.

    This isolates "does role-splitting alone buy safety?" — the answer the
    experiment demonstrates is NO. Routing can be correct while the injected
    out-of-scope action still executes, because there is no aggregate_verdict
    boundary to consult.
    """
    outcomes: list[CaseOutcome] = []
    for c in cases:
        task_text = resolve_injection(c)
        routed = _keyword_router(task_text)
        executed = _naive_execute(task_text) if routed is not None else False
        outcomes.append(
            CaseOutcome(
                case_id=c.id,
                arm="B",
                effective_decision="allow",  # no charter -> no denial path
                routed_to=routed,
                executed=executed,
                grant_applied=False,
                conflict_ms=None,  # no needs_approval concept without a charter
                notes="multi-agent no-charter: routes but cannot gate",
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# Optional: live (Qwen-backed) variants — seam, not implemented here
# ---------------------------------------------------------------------------


def run_arm_b_live(cases: list[TaskCase], grader: object) -> list[CaseOutcome]:  # noqa: ARG001
    """Placeholder for a Qwen-reasoned router that STILL has no charter gate.

    Swapping in a real LLM router would improve arm B's route_accuracy toward
    arm C's, sharpening the story: even with a smart router, the missing
    *charter boundary* is what leaves intercept_rate at ~0. Implemented by
    wp-experiment once wp-qwen's make_qwen_grader() lands; for now arm B runs
    the deterministic keyword router so the harness is runnable offline.
    """
    raise NotImplementedError(
        "Live arm-B router is wired once wp-qwen make_qwen_grader() is available; "
        "use run_arm_b for the deterministic offline experiment."
    )


def _timed(fn, *args):  # noqa: ANN001, ANN202
    """Tiny wall-clock helper so live arms can populate conflict_ms uniformly."""
    t0 = time.perf_counter()
    result = fn(*args)
    return result, (time.perf_counter() - t0) * 1000.0
