"""CFO Orchestrator — the multi-agent task router for the CFO Office showcase.

This is the **wp-orchestrator** deliverable. It is the "principal-side" driver
that turns one complex task ("Complete Q2 tax filing, then notify the external
auditor") into a governed sequence of delegations to four worker agents, each
of which holds its own signed Charter.

Design altitude (read this first)
=================================
The orchestrator is deliberately *dumb about policy*. It NEVER decides
allow / needs_approval / incompatible itself — that judgment is the protocol
layer's job:

    fetch_charter (data)  ->  grader (LLM marks clause hits)  ->  aggregate_verdict
    (deterministic TYPE_TO_DECISION + `incompatible > needs_approval > allow`)

The orchestrator only *reacts* to the Verdict the protocol produces:

    allow            -> let the worker execute the step
    needs_approval   -> pause; emit a StepUpRequest to the principal-approval
                        callback; if the principal signs an AdHocGrant, re-gate
                        via apply_grant and (only if it downgrades to allow)
                        retry the step ONCE with grant_id attached
    incompatible     -> TERMINAL. log + skip. Never grantable, never retried.
                        (out_of_scope is structurally un-relaxable — this is the
                        red line the whole submission rests on.)

Every transition is written to a transparency log so the demo can replay an
auditable timeline.

What this module owns vs. depends on
====================================
Owns (this file + siblings):
    - CFOOrchestrator           the decompose -> route -> gate -> escalate loop
    - RunResult / StepOutcome   the structured run report
    - the transparency-log append for orchestration events

Depends on (other work packages — imported behind soft boundaries so this
skeleton runs even before they land):
    - wp-charters   seed_cfo_office(base_url) -> dict[agent_id -> Charter]
    - wp-qwen       make_qwen_grader() -> HitsGrader   (falls back to the
                    anthropic grader, or a deterministic stub, when unset)
    - wp-stepup     StepUpRequest / AdHocGrant / GrantVerdict + the
                    request_step_up / apply_grant MCP tools
    - workers.py    worker(agent_id, charter, task_envelope, grader) -> outbox dict
    - task_plan.py  the Q2-tax DAG (TaskPlan / TaskStep)

EXISTING charter MCP tools are reused verbatim and unwrapped through the
`_call_tool('fn'|'func'|'__wrapped__')` pattern proven in scripts/demo_chain.py:
    delegate_task, check_inbox, send_result, read_outbox, aggregate_verdict.
No protocol bytes are changed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# --- EXISTING protocol surface (confirmed against the real source) ----------
# These imports are the load-bearing contract. Their signatures were verified
# against charter/mcp_server.py, charter/schema.py, charter/storage.py.
from charter.mcp_server import (
    aggregate_verdict as _aggregate_verdict_tool,
)
from charter.mcp_server import (
    delegate_task as _delegate_task_tool,
)
from charter.mcp_server import (
    read_outbox as _read_outbox_tool,
)
from charter.mcp_server import (
    send_result as _send_result_tool,
)
from charter.schema import Charter
from charter.storage import data_root

if TYPE_CHECKING:
    # Only for type hints — kept out of runtime import graph so this skeleton
    # does not hard-fail if wp-qwen / wp-stepup have not landed yet.
    from charter.adapters.openai_agents import HitsGrader

    from .task_plan import TaskPlan, TaskStep


# ===========================================================================
# Tool-unwrap helper (mirrors scripts/demo_chain.py::_call_tool verbatim)
# ===========================================================================
def _call_tool(tool: Any, *args: Any, **kwargs: Any) -> Any:
    """Call an @mcp.tool-decorated function by unwrapping the FastMCP wrapper.

    FastMCP wraps the raw function; the original is reachable under one of
    ``fn`` / ``func`` / ``__wrapped__``. This is the exact pattern the shipped
    demo_chain.py uses, so it is known-good against the installed fastmcp shim.
    """
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


# ===========================================================================
# Soft boundaries to not-yet-landed work packages (wp-stepup, wp-qwen, workers)
# ===========================================================================
def _try_import_stepup() -> Any | None:
    """Return a step-up tool bundle if wp-stepup has landed, else None.

    wp-stepup ships two halves (verified against the real source):
      - the MCP tools  `request_step_up` / `apply_grant`  live in
        `charter.mcp_server` (appended as tools #12/#13).
      - the Python API `issue_grant` / `GrantConstraints` / `AdHocGrant` etc.
        live in `charter.stepup`.

    We bundle the pieces the orchestrator needs into one namespace object so the
    rest of the file has a single soft-import seam. Returns None (and the
    orchestrator pauses every needs_approval step — the safe default) if either
    half is missing.
    """
    try:
        import charter.stepup as _stepup_mod  # type: ignore[import-not-found]
        from charter.mcp_server import apply_grant as _apply_grant_tool
        from charter.mcp_server import request_step_up as _request_step_up_tool
    except Exception:  # pragma: no cover - exercised only before wp-stepup lands
        return None

    bundle = type("_StepUpBundle", (), {})()
    bundle.request_step_up = _request_step_up_tool  # MCP tool (unwrap via _call_tool)
    bundle.apply_grant = _apply_grant_tool  # MCP tool (unwrap via _call_tool)
    bundle.issue_grant = _stepup_mod.issue_grant  # principal-side signer
    bundle.GrantConstraints = _stepup_mod.GrantConstraints
    return bundle


def _resolve_default_grader() -> HitsGrader:
    """Pick the grader per CHARTER_LLM_PROVIDER, falling back safely.

    Resolution order:
      1. CHARTER_LLM_PROVIDER=qwen  -> charter.adapters.qwen.make_qwen_grader()
      2. anthropic default          -> charter.loopback._grade_via_llm
      3. neither importable         -> a deterministic keyword stub so the
                                       no-LLM demo path still produces hits.

    The orchestrator accepts an explicit grader in its constructor; this is
    only the fallback used when the caller passes grader=None.
    """
    provider = os.environ.get("CHARTER_LLM_PROVIDER", "anthropic").lower()

    if provider == "qwen":
        try:
            from charter.adapters.qwen import make_qwen_grader  # type: ignore

            return make_qwen_grader()
        except Exception:
            pass  # fall through to anthropic / stub

    try:
        from charter.loopback import _grade_via_llm

        return _grade_via_llm
    except Exception:
        return _stub_grader


def _stub_grader(charter: Charter, task: str) -> list[dict[str, Any]]:
    """Deterministic no-LLM grader for the reproducible demo/video path.

    Marks a clause hit when any "trigger" keyword associated with the clause
    text appears in the task. This is intentionally crude — it exists so the
    decompose->gate->escalate loop is exercisable with zero API keys and zero
    network. wp-qwen / the anthropic grader replace it in real runs.

    Returns the same shape every real grader returns:
        [{id, hit: bool, confidence: float, reason: str}, ...]
    """
    task_l = task.lower()
    hits: list[dict[str, Any]] = []
    for clause in charter.clauses:
        # naive bag-of-words overlap on the clause text
        words = {w for w in clause.text.lower().replace(",", " ").split() if len(w) > 3}
        overlap = [w for w in words if w in task_l]
        if overlap:
            hits.append(
                {
                    "id": clause.id,
                    "hit": True,
                    "confidence": 0.9,
                    "reason": f"task mentions {overlap[:3]} (clause {clause.id})",
                }
            )
    return hits


# ===========================================================================
# Result data structures (the orchestrator's public report shape)
# ===========================================================================
@dataclass
class StepOutcome:
    """One step's journey through the gate. Serializable for the transcript."""

    step_id: str
    agent_id: str
    intended_task: str
    task_id: str | None = None
    verdict: dict[str, Any] | None = None  # the Verdict dict (decision/...)
    effective_decision: str | None = None  # post-grant decision if escalated
    executed: bool = False
    execution_output: str | None = None
    grant_id: str | None = None
    skipped: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "intended_task": self.intended_task,
            "task_id": self.task_id,
            "verdict": self.verdict,
            "effective_decision": self.effective_decision,
            "executed": self.executed,
            "execution_output": self.execution_output,
            "grant_id": self.grant_id,
            "skipped": self.skipped,
            "note": self.note,
        }


@dataclass
class RunResult:
    """The full run report returned by CFOOrchestrator.run()."""

    main_task: str
    steps: list[StepOutcome] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)  # AdHocGrant dicts
    final_status: str = "unknown"  # completed | blocked | partial

    def to_dict(self) -> dict[str, Any]:
        return {
            "main_task": self.main_task,
            "final_status": self.final_status,
            "steps": [s.to_dict() for s in self.steps],
            "escalations": self.escalations,
        }


# The principal-approval callback. Given a StepUpRequest dict, the principal
# (a human, or in the demo an auto-approver with policy) returns either a
# signed AdHocGrant dict to authorize the single needs_approval step, or None
# to deny. The orchestrator NEVER fabricates a grant itself.
ApprovalCallback = Callable[[dict[str, Any]], dict[str, Any] | None]


# ===========================================================================
# Transparency log for orchestration events (additive; not the charter log)
# ===========================================================================
def _orchestrator_log_path() -> Path:
    """Append-only JSONL audit trail for orchestration events.

    Distinct from the protocol's signed-charter transparency log
    (charter.transparency). That log records charter ISSUANCE; this one records
    ROUTING decisions (decompose/route/gate/escalate/grant/execute/skip) so the
    demo can render a human-readable audit timeline. Lives next to the
    inbox/outbox files under data/messages/ so the whole conversation trail is
    co-located.
    """
    path = data_root() / "messages" / "transparency_orchestrator.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ===========================================================================
# The orchestrator
# ===========================================================================
class CFOOrchestrator:
    """Decompose -> route -> gate -> escalate -> complete.

    Args:
        charters: dict[agent_id -> Charter], as returned by
                  wp-charters.seed_cfo_office(base_url). The orchestrator
                  looks the worker's Charter up by agent_id when gating.
        grader:   the HitsGrader used by workers to mark clause hits. Same
                  grader is shared across all workers so the only governance
                  variable in the A/B/C experiment is the charter layer, not
                  the model. If None, resolved via CHARTER_LLM_PROVIDER.
        base_url: charter URL base; binding URLs are
                  f"{base_url}/{principal_id}/{agent_id}" (matches
                  delegate_task). Also written into CHARTER_URL_BASE so the
                  unwrapped delegate_task builds the same URL.
        approval_cb: principal-approval callback for needs_approval steps.
                     If None, every needs_approval step stays paused (the
                     conservative default — nothing is auto-granted).
        principal_id: the root principal id used as `from_agent` on delegations
                      and as the grant issuer identity. Default "cfo_office".
    """

    def __init__(
        self,
        charters: dict[str, Charter],
        grader: HitsGrader | None = None,
        base_url: str = "http://localhost:8000",
        *,
        approval_cb: ApprovalCallback | None = None,
        principal_id: str = "cfo_office",
    ) -> None:
        self.charters = charters
        self.grader: HitsGrader = grader or _resolve_default_grader()
        self.base_url = base_url.rstrip("/")
        self.approval_cb = approval_cb
        self.principal_id = principal_id
        self._stepup = _try_import_stepup()

        # Make delegate_task build URLs against the same base we gate against.
        os.environ["CHARTER_URL_BASE"] = self.base_url

        # In-run accumulator of completed step outputs, so a downstream step's
        # `context` can be populated from upstream `execution_output`.
        self._completed: dict[str, StepOutcome] = {}

    # -- public entry point --------------------------------------------------
    def run(self, main_task: str | TaskPlan) -> RunResult:
        """Run a TaskPlan (or a plain string) end to end.

        A bare string is decomposed via `decompose()`; a TaskPlan is used as
        given (the canonical Q2-tax DAG comes pre-decomposed from task_plan.py).
        Steps run in dependency order; a step whose upstream was skipped/blocked
        is itself skipped (we never execute a step on stale/absent context).
        """
        plan = self._as_plan(main_task)
        result = RunResult(main_task=plan.title)
        self._log_event("decompose", {"title": plan.title, "n_steps": len(plan.steps)})

        for step in plan.steps:
            if not self._deps_satisfied(step):
                outcome = StepOutcome(
                    step_id=step.step_id,
                    agent_id=step.agent_id,
                    intended_task=step.intended_task,
                    skipped=True,
                    note="upstream dependency was blocked or skipped",
                )
                self._record(result, outcome)
                continue

            outcome = self._run_step(plan, step, result)
            self._record(result, outcome)

        result.final_status = self._final_status(result)
        self._log_event("run_complete", {"final_status": result.final_status})
        return result

    # -- one step: route -> gate -> (escalate) -> execute --------------------
    def _run_step(self, plan: TaskPlan, step: TaskStep, result: RunResult) -> StepOutcome:
        outcome = StepOutcome(
            step_id=step.step_id,
            agent_id=step.agent_id,
            intended_task=step.intended_task,
        )

        charter = self.charters.get(step.agent_id)
        if charter is None:
            outcome.skipped = True
            outcome.note = f"no charter registered for agent_id={step.agent_id!r}"
            self._log_event("route_failed", outcome.to_dict())
            return outcome

        # 1) ROUTE: hand the task to the worker via the real delegate_task tool.
        envelope = self._delegate(step)
        outcome.task_id = envelope["task_id"]
        self._log_event("route", {"step_id": step.step_id, "agent_id": step.agent_id,
                                   "task_id": outcome.task_id, "charter_url": envelope["charter_url"]})

        # 2) GATE: the worker fetches its charter, grades hits, aggregates a
        #    verdict, and writes the result to the outbox. We read it back.
        outbox = self._invoke_worker(step, charter, envelope)
        verdict = outbox["verdict"]
        outcome.verdict = verdict
        outcome.effective_decision = verdict["decision"]
        self._log_event("gate", {"step_id": step.step_id, "decision": verdict["decision"],
                                  "applied": _applied_ids(verdict)})

        decision = verdict["decision"]

        if decision == "allow":
            outcome.executed = bool(outbox.get("executed"))
            outcome.execution_output = outbox.get("execution_output")
            self._log_event("execute", {"step_id": step.step_id, "executed": outcome.executed})
            return outcome

        if decision == "incompatible":
            # RED LINE: terminal. out_of_scope is never grantable. We do not
            # retry, do not propose, do not escalate. Log and skip.
            outcome.skipped = True
            outcome.note = "incompatible (out_of_scope) — terminal, not grantable"
            self._log_event("blocked_incompatible", outcome.to_dict())
            return outcome

        # decision == "needs_approval": ESCALATE via step-up negotiation.
        return self._escalate(step, charter, envelope, outbox, outcome, result)

    # -- escalation: StepUpRequest -> principal grant -> apply_grant ---------
    def _escalate(
        self,
        step: TaskStep,
        charter: Charter,
        envelope: dict[str, Any],
        outbox: dict[str, Any],
        outcome: StepOutcome,
        result: RunResult,
    ) -> StepOutcome:
        """Handle a needs_approval verdict through the step-up protocol.

        Strict ordering, all guarded by wp-stepup's red lines:
            request_step_up(charter_url, intended_task, failed_verdict)
              -> refuses unless decision == needs_approval (structural)
            approval_cb(step_up_request)            -> principal signs AdHocGrant
            apply_grant(charter, hits, grant)       -> recompute base verdict,
              downgrade ONLY covered needs_approval clauses; incompatible stays
              incompatible regardless of grant.
        The orchestrator forwards and re-gates; it never downgrades itself.
        """
        t0 = time.monotonic()

        if self._stepup is None or self.approval_cb is None:
            outcome.skipped = True
            outcome.note = (
                "needs_approval but no step-up path available "
                f"(stepup={'present' if self._stepup else 'absent'}, "
                f"approval_cb={'set' if self.approval_cb else 'unset'}) — paused"
            )
            self._log_event("paused_needs_approval", outcome.to_dict())
            return outcome

        verdict = outbox["verdict"]

        # (a) Build the escalation payload via the real request_step_up tool.
        #     Signature (verified): request_step_up(charter_url, intended_task,
        #     failed_verdict, task_id="", justification=""). It REFUSES anything
        #     that is not needs_approval (structural red line) and returns
        #     {ok, step_up_request: {...}} on success.
        step_up_res = _call_tool(
            self._stepup.request_step_up,
            envelope["charter_url"],
            step.intended_task,
            verdict,
            envelope["task_id"],
            f"Q2-tax DAG step {step.step_id} requires principal approval.",
        )
        if not isinstance(step_up_res, dict) or step_up_res.get("ok") is not True:
            reason = step_up_res.get("reason") if isinstance(step_up_res, dict) else step_up_res
            outcome.skipped = True
            outcome.note = f"request_step_up refused: {reason}"
            self._log_event("stepup_refused", outcome.to_dict())
            return outcome

        step_up_payload = step_up_res["step_up_request"]
        self._log_event("stepup_request", {"step_id": step.step_id,
                                            "requested_clause_ids": step_up_payload.get(
                                                "requested_clause_ids")})

        # (b) Ask the principal. The callback returns a signed AdHocGrant dict,
        #     or None to deny. We NEVER mint a grant ourselves.
        grant = self.approval_cb(step_up_payload)
        if grant is None:
            outcome.skipped = True
            outcome.note = "principal denied step-up; needs_approval not waived"
            self._log_event("grant_denied", outcome.to_dict())
            return outcome

        grant_dict = _as_dict(grant)
        result.escalations.append(grant_dict)
        outcome.grant_id = grant_dict.get("grant_id")
        self._log_event("grant_issued", {"step_id": step.step_id,
                                          "grant_id": outcome.grant_id,
                                          "relaxes_clause_ids": grant_dict.get(
                                              "relaxes_clause_ids")})

        # (c) RE-GATE under the grant. apply_grant recomputes the base verdict
        #     and only downgrades covered needs_approval clauses. Incompatible
        #     stays incompatible (red line, enforced inside apply_grant).
        #     Signature (verified): apply_grant(charter, hits, grant,
        #     charter_url="", task_id="", recipients=None, budget_usd=None).
        hits = outbox.get("hits") or self.grader(charter, step.intended_task)
        recipients = _recipients_for(step)
        grant_verdict = _as_dict(
            _call_tool(
                self._stepup.apply_grant,
                charter.model_dump(mode="json"),
                hits,
                grant_dict,
                envelope["charter_url"],
                envelope["task_id"],
                recipients,
            )
        )
        effective = grant_verdict.get("effective_decision")
        outcome.effective_decision = effective
        self._log_event("apply_grant", {"step_id": step.step_id,
                                         "granted": grant_verdict.get("granted"),
                                         "effective_decision": effective})

        conflict_ms = int((time.monotonic() - t0) * 1000)
        self._log_event("conflict_resolved", {"step_id": step.step_id,
                                               "conflict_resolution_ms": conflict_ms})

        if effective != "allow":
            # Grant did not (or could not) downgrade to allow — stays paused.
            outcome.skipped = True
            outcome.note = f"grant did not yield allow (effective={effective}); not executed"
            self._log_event("grant_insufficient", outcome.to_dict())
            return outcome

        # (d) Retry the step ONCE, now carrying the grant_id so the worker (and
        #     audit log) can see it executed under a one-shot authorization.
        retry_envelope = self._delegate(step, grant_id=outcome.grant_id)
        outcome.task_id = retry_envelope["task_id"]
        retry_outbox = self._invoke_worker(
            step, charter, retry_envelope, force_allow=True, grant_id=outcome.grant_id
        )
        outcome.executed = bool(retry_outbox.get("executed"))
        outcome.execution_output = retry_outbox.get("execution_output")
        self._log_event("execute_under_grant", {"step_id": step.step_id,
                                                 "grant_id": outcome.grant_id,
                                                 "executed": outcome.executed})
        return outcome

    # -- worker invocation ---------------------------------------------------
    def _invoke_worker(
        self,
        step: TaskStep,
        charter: Charter,
        envelope: dict[str, Any],
        *,
        force_allow: bool = False,
        grant_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the worker for this step and return its outbox dict.

        Delegates to workers.worker(agent_id, charter, task_envelope, grader)
        when workers.py is available; otherwise runs an inline fallback worker
        so the orchestrator skeleton is runnable on its own. Either way the
        worker is responsible for grading + aggregate_verdict + (on allow)
        executing, then writing send_result. We read_outbox back.

        `force_allow` is set only on the post-grant retry: the gate already
        passed via apply_grant, so the worker should execute rather than
        re-deny on the same (now-waived) needs_approval clause.
        """
        try:
            from .workers import worker as _worker  # wp-orchestrator sibling

            outbox = _worker(
                step.agent_id,
                charter,
                _envelope_with(envelope, step, grant_id=grant_id, force_allow=force_allow),
                self.grader,
            )
        except Exception:
            outbox = self._inline_worker(
                step, charter, envelope, force_allow=force_allow, grant_id=grant_id
            )

        # Mirror the worker reply into the real outbox file for the audit trail,
        # then read it back through the real tool (round-trips the JSON shape).
        _call_tool(
            _send_result_tool,
            outbox["task_id"],
            outbox["verdict"],
            outbox.get("response_text", ""),
            outbox.get("executed", False),
            outbox.get("execution_output"),
            step.agent_id,
        )
        persisted = _call_tool(_read_outbox_tool) or outbox
        # carry hits forward (not persisted by send_result) for apply_grant reuse
        persisted.setdefault("hits", outbox.get("hits"))
        return persisted

    def _inline_worker(
        self,
        step: TaskStep,
        charter: Charter,
        envelope: dict[str, Any],
        *,
        force_allow: bool,
        grant_id: str | None,
    ) -> dict[str, Any]:
        """Self-contained worker fallback (used until workers.py lands).

        Grades hits with self.grader, aggregates via the REAL aggregate_verdict
        tool (so the deterministic protocol math is exercised), then "executes"
        deterministically on allow / forced-allow.
        """
        hits = self.grader(charter, step.intended_task)
        verdict = _call_tool(
            _aggregate_verdict_tool, charter.model_dump(mode="json"), hits
        )
        decision = "allow" if force_allow else verdict["decision"]
        executed = decision == "allow"
        output = (
            f"[stub-exec] {step.agent_id} ran step {step.step_id}: "
            f"{step.intended_task}" + (f" (grant {grant_id})" if grant_id else "")
            if executed
            else None
        )
        return {
            "task_id": envelope["task_id"],
            "from_agent": step.agent_id,
            "verdict": verdict,
            "hits": hits,
            "response_text": f"{step.agent_id} verdict={verdict['decision']}",
            "executed": executed,
            "execution_output": output,
            "step_id": step.step_id,
        }

    # -- delegation ----------------------------------------------------------
    def _delegate(self, step: TaskStep, *, grant_id: str | None = None) -> dict[str, Any]:
        """Send the step to its worker via the real delegate_task tool.

        Returns the tool's dict ({task_id, charter_url, ...}). The additive
        envelope fields (step_id/depends_on/context/grant_id) are tracked
        orchestrator-side; the base tool ignores them (back-compat).
        """
        charter = self.charters[step.agent_id]
        target_agent_id = charter.binding.agent_id
        target_principal_id = charter.binding.principal_id

        res = _call_tool(
            _delegate_task_tool,
            target_principal_id,
            target_agent_id,
            step.intended_task,
            self.principal_id,  # from_agent = the CFO Office principal
        )
        if grant_id:
            res = {**res, "grant_id": grant_id}
        return res

    # -- bookkeeping helpers -------------------------------------------------
    def _deps_satisfied(self, step: TaskStep) -> bool:
        for dep in getattr(step, "depends_on", []) or []:
            up = self._completed.get(dep)
            if up is None or up.skipped or not up.executed:
                return False
        return True

    def _record(self, result: RunResult, outcome: StepOutcome) -> None:
        result.steps.append(outcome)
        self._completed[outcome.step_id] = outcome

    def _final_status(self, result: RunResult) -> str:
        if all(s.executed for s in result.steps):
            return "completed"
        if any(s.executed for s in result.steps):
            return "partial"
        return "blocked"

    def _as_plan(self, main_task: str | TaskPlan) -> TaskPlan:
        if isinstance(main_task, str):
            from .task_plan import decompose

            return decompose(main_task)
        return main_task

    def _log_event(self, event: str, payload: dict[str, Any]) -> None:
        entry = {
            "ts": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "event": event,
            "run_id": getattr(self, "_run_id", None),
            **payload,
        }
        with _orchestrator_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ===========================================================================
# small free helpers
# ===========================================================================
def _applied_ids(verdict: dict[str, Any]) -> list[str]:
    return [m.get("id") for m in verdict.get("matched_clauses", []) if m.get("applied")]


def _as_dict(obj: Any) -> dict[str, Any]:
    """Coerce a wp-stepup pydantic model OR an already-dict tool result to dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return dict(obj)


def _recipients_for(step: TaskStep) -> list[str] | None:
    """Concrete recipients of a step's action, for the grant's allowlist check.

    apply_grant enforces `constraints.allowed_recipients ⊇ recipients`. For the
    external-send step we surface the auditor address so the grant's recipient
    constraint is actually exercised (not just declared).
    """
    if "auditor" in step.intended_task.lower():
        return ["auditor@external-firm.com"]
    return None


def _charter_for_url(charters: dict[str, Charter], charter_url: str) -> Charter | None:
    """Find the seeded Charter whose binding URL matches `charter_url`.

    Binding URL convention (matches delegate_task):
        f"{base}/{principal_id}/{agent_id}"  ->  ends with "/<agent_id>".
    """
    if not charter_url:
        return None
    agent_id = charter_url.rstrip("/").rsplit("/", 1)[-1]
    return charters.get(agent_id)


def _envelope_with(
    envelope: dict[str, Any],
    step: TaskStep,
    *,
    grant_id: str | None,
    force_allow: bool,
) -> dict[str, Any]:
    """Build the additive task envelope passed to workers.worker().

    EXISTING base fields (task_id, charter_url, intended_task, ...) are kept
    verbatim; CFO-office ADDITIVE optional fields are layered on top. The base
    mcp_server ignores them, so delegate_task/check_inbox stay byte-compatible.
    """
    return {
        **envelope,
        "step_id": step.step_id,
        "depends_on": list(getattr(step, "depends_on", []) or []),
        "context": dict(getattr(step, "context", {}) or {}),
        "grant_id": grant_id,
        "force_allow": force_allow,
    }


# ===========================================================================
# run_demo — the in-process end-to-end entry point
# ===========================================================================
def run_demo(live_llm: bool = False) -> int:
    """Seed the 5 charters, run the canonical Q2-tax DAG, print the timeline.

    This is the orchestrator's own smoke entry (wp-demo-script ships the richer
    narrated demo.py / run_demo.py CLI). It runs fully in-process with the
    deterministic stub grader unless `live_llm` is True and a provider is set,
    so it is safe for the <3-min reproducible video take.

    Returns a process exit code (0 = the run reached a terminal state cleanly).

    The step-up tools (`request_step_up` / `apply_grant`) re-fetch and re-verify
    the worker Charter over HTTP, so the run happens INSIDE a live in-process
    charter.server fixture (the same `uvicorn`-in-a-thread pattern shipped in
    scripts/demo_chain.py). This makes the escalation beat actually execute
    end-to-end rather than fail-closed on an unreachable charter_url.
    """
    with _live_server() as base_url:
        os.environ["CHARTER_URL_BASE"] = base_url

        # 1) Charters. Prefer wp-charters' seeder; fall back to a tiny inline
        #    seed so the skeleton demonstrates the loop even in isolation. Both
        #    paths save signed Charters to disk so the live server can serve them.
        try:
            from .charters.seed_cfo_office import seed_cfo_office

            charters = seed_cfo_office(base_url=base_url)
        except Exception as exc:
            print(f"[run_demo] seed_cfo_office unavailable ({exc!r}); using inline demo charters")
            charters = _inline_demo_charters(base_url)

        # 2) Grader. Stub unless live_llm requested.
        grader: HitsGrader = _resolve_default_grader() if live_llm else _stub_grader

        # 3) Principal-approval callback: a policy auto-approver that mints an
        #    AdHocGrant ONLY for the external-send step, scoped to the auditor.
        approval_cb = _make_demo_approval_cb(charters)

        # 4) The canonical main task.
        try:
            from .task_plan import build_q2_tax_plan

            main_task: Any = build_q2_tax_plan()
        except Exception:
            main_task = "Complete Q2 tax filing, then notify the external auditor."

        orch = CFOOrchestrator(
            charters, grader=grader, base_url=base_url, approval_cb=approval_cb
        )
        orch._run_id = uuid.uuid4().hex[:8]
        result = orch.run(main_task)

        _print_timeline(result)
        return 0 if result.final_status in ("completed", "partial") else 1


@contextmanager
def _live_server() -> Iterator[str]:
    """Serve charter.server.app on a free localhost port in a daemon thread.

    Verbatim port of scripts/demo_chain.py::_live_server so the step-up tools'
    HTTP fetch of each worker Charter resolves against a real endpoint. Yields
    the base URL. wp-demo-script's demo.py may host this differently; this
    fixture only exists so the orchestrator's own smoke entry is self-contained.
    """
    import socket

    import uvicorn

    from charter.server import app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("demo server did not start within 2.5s")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def _make_demo_approval_cb(
    charters: dict[str, Charter] | None = None,
    principal_id: str = "cfo_office",
) -> ApprovalCallback:
    """A demo principal-approval policy.

    Approves exactly the needs_approval steps the CFO would sign off on
    (external auditor notification) and denies the rest. On approval it mints a
    REAL signed AdHocGrant via `stepup.issue_grant`, scoped to one send to the
    auditor, using the principal's own ed25519 key. The grant relaxes ONLY the
    needs_approval clause ids the StepUpRequest asked for — `issue_grant` runs
    `validate_grant_targets` first, so an attempt to relax an out_of_scope
    clause would raise before any grant is produced (red line layer 1).
    """

    def _cb(step_up_request: dict[str, Any]) -> dict[str, Any] | None:
        task = (step_up_request.get("intended_task") or "").lower()
        # Policy: only auto-approve the external-auditor notification beat.
        if "auditor" not in task and "external" not in task:
            return None

        stepup = _try_import_stepup()
        if stepup is None:
            return None  # no grant machinery -> deny (fail-closed)

        # Resolve the charter the grant is scoped to (so we can canonicalize it
        # and borrow the principal's key for the same issuer identity).
        charter_url = step_up_request.get("charter_url", "")
        charter = _charter_for_url(charters or {}, charter_url)
        if charter is None:
            return None

        from charter.signing import public_key_to_string
        from charter.storage import ensure_issuer_key

        private_key = ensure_issuer_key(principal_id)
        issuer_pk = public_key_to_string(private_key.public_key())

        constraints = stepup.GrantConstraints(
            one_shot=True,
            allowed_recipients=["auditor@external-firm.com"],
            ttl_seconds=600,
        )
        try:
            grant = stepup.issue_grant(
                charter=charter.model_dump(mode="json"),
                charter_url=charter_url,
                task_id=step_up_request.get("task_id", ""),
                relaxes_clause_ids=list(step_up_request.get("requested_clause_ids", [])),
                private_key=private_key,
                issuer_public_key=issuer_pk,
                reason="CFO approved a single external-auditor notification for the Q2 filing.",
                constraints=constraints,
                issued_by=principal_id,
            )
        except ValueError:
            # validate_grant_targets refused (a non-needs_approval clause was
            # requested) — deny rather than mint a malformed grant.
            return None
        return _as_dict(grant)

    return _cb


def _inline_demo_charters(base_url: str) -> dict[str, Charter]:
    """Minimal inline charter seed so the skeleton runs before wp-charters.

    Builds + signs two of the four workers (enough to exercise allow +
    needs_approval). Reuses the EXACT build pattern from scripts/seed_demo.py.
    """
    from datetime import timedelta

    from charter.schema import (
        AgentOperator,
        Binding,
        Clause,
        Issuer,
        Lifecycle,
        Principal,
        Provenance,
        Summary,
    )
    from charter.signing import public_key_to_string, sign_charter
    from charter.storage import ensure_issuer_key, save_charter

    def _build(principal_id: str, agent_id: str, clauses: list[Clause], summary: str) -> Charter:
        now = datetime.now(UTC).replace(microsecond=0)
        pk = ensure_issuer_key(principal_id)
        charter = Charter(
            charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
            binding=Binding(principal_id=principal_id, agent_id=agent_id),
            principal=Principal(id=principal_id, role_summary="CFO Office worker"),
            issuer=Issuer(id=principal_id, relationship_to_principal="self"),
            agent_operator=AgentOperator(id="generic_worker_agent_provider"),
            summary=Summary(plain_language=summary),
            clauses=clauses,
            lifecycle=Lifecycle(
                issued_at=now, valid_until=now + timedelta(days=30), status="active"
            ),
            provenance=Provenance(
                issuer_public_key=public_key_to_string(pk.public_key()),
                issuer_signature="",
                generated_at=now,
            ),
        )
        sign_charter(charter, pk)
        save_charter(charter)
        return charter

    analyst = _build(
        "cfo_office",
        "data_analyst_agent_v1",
        [
            Clause(id="C-001", type="scope", text="Read-only ledger summary export and analysis."),
            Clause(id="C-101", type="out_of_scope", text="Any write or mutation of data."),
        ],
        "Data-analyst worker: read-only ledger work in scope; writes out of scope.",
    )
    bookkeeping = _build(
        "cfo_office",
        "bookkeeping_agent_v1",
        [
            Clause(id="C-001", type="scope", text="Bookkeeping, invoice reconciliation, expense classify."),
        ],
        "Bookkeeping worker: reconcile invoices and classify expenses.",
    )
    tax = _build(
        "cfo_office",
        "tax_filing_agent_v1",
        [
            Clause(id="C-001", type="scope", text="Quarterly tax filing, tax compute, reconciled ledger."),
            Clause(
                id="C-201",
                type="approval_required",
                text="Any destructive database action — DROP TABLE, DELETE, TRUNCATE — requires approval.",
            ),
        ],
        "Tax-filing worker: tax filing in scope; destructive DB actions need approval.",
    )
    comms = _build(
        "cfo_office",
        "comms_agent_v1",
        [
            Clause(id="C-001", type="scope", text="Sending email, notification, auditor notify."),
            Clause(
                id="C-202",
                type="approval_required",
                text="Sending email to external auditor or outside recipients requires approval.",
            ),
            Clause(id="C-101", type="out_of_scope", text="Anything that is not email."),
        ],
        "Comms worker: email in scope; external sends need approval; non-email out of scope.",
    )
    return {
        "data_analyst_agent_v1": analyst,
        "bookkeeping_agent_v1": bookkeeping,
        "tax_filing_agent_v1": tax,
        "comms_agent_v1": comms,
    }


def _print_timeline(result: RunResult) -> None:
    print("=" * 72)
    print(f"CFO Orchestrator — {result.main_task}")
    print(f"final_status: {result.final_status}")
    print("=" * 72)
    for s in result.steps:
        dec = (s.effective_decision or "?").upper()
        flag = "EXECUTED" if s.executed else ("SKIPPED" if s.skipped else "PAUSED")
        line = f"[{s.step_id}] {s.agent_id:>22}  ->  {dec:<14} {flag}"
        if s.grant_id:
            line += f"  grant={s.grant_id}"
        print(line)
        if s.note:
            print(f"      note: {s.note}")
    if result.escalations:
        print("-" * 72)
        print(f"escalations (AdHocGrants minted): {len(result.escalations)}")
    print(f"audit log: {_orchestrator_log_path()}")


if __name__ == "__main__":
    import sys

    sys.exit(run_demo(live_llm=False))
