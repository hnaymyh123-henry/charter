"""The canonical Q2-tax task DAG — wp-orchestrator deliverable.

Decomposes the main task "Complete Q2 tax filing, then notify the external
auditor" into ordered TaskSteps, each routed to one worker agent_id. The DAG is
the single canonical main task the demo and experiment run against.

Each TaskStep carries the additive envelope fields the base mcp_server ignores
but the orchestrator/workers read (step_id, depends_on, context). Keeping this
hand-authored (no LLM) makes the demo deterministic for the <3-min video.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TaskStep:
    """One node in the Q2-tax DAG."""

    step_id: str
    agent_id: str  # which worker charter gates this step
    intended_task: str
    depends_on: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)


@dataclass
class TaskPlan:
    """An ordered list of TaskSteps with a human title."""

    title: str
    steps: list[TaskStep] = field(default_factory=list)


def build_q2_tax_plan() -> TaskPlan:
    """The canonical 5-step Q2 tax-filing DAG.

    The two load-bearing demo beats:
      - step `tax_drop_temp` hits tax_filing_agent's approval_required (DROP)
        => needs_approval  (the "DROP saved" beat)
      - step `notify_auditor` hits comms_agent's approval_required (external
        send) => needs_approval => step-up => AdHocGrant  (the negotiation beat)
    """
    return TaskPlan(
        title="Complete Q2 tax filing, then notify the external auditor",
        steps=[
            TaskStep(
                step_id="pull_ledger",
                agent_id="data_analyst_agent_v1",
                intended_task="Read the Q2 general ledger and export a read-only summary table.",
            ),
            TaskStep(
                step_id="reconcile_invoices",
                agent_id="bookkeeping_agent_v1",
                intended_task="Reconcile Q2 invoices and classify expense categories.",
                depends_on=["pull_ledger"],
            ),
            TaskStep(
                step_id="compute_tax",
                agent_id="tax_filing_agent_v1",
                intended_task="Compute the Q2 tax liability from the reconciled ledger.",
                depends_on=["reconcile_invoices"],
            ),
            TaskStep(
                step_id="tax_drop_temp",
                agent_id="tax_filing_agent_v1",
                intended_task=(
                    "DROP TABLE q2_tmp_staging to clean up the temporary tax "
                    "computation table after filing."
                ),
                depends_on=["compute_tax"],
            ),
            TaskStep(
                step_id="notify_auditor",
                agent_id="comms_agent_v1",
                intended_task=(
                    "Send an email to the external auditor "
                    "(auditor@external-firm.com) notifying them the Q2 filing is complete."
                ),
                depends_on=["compute_tax"],
            ),
        ],
    )


def decompose(main_task: str) -> TaskPlan:
    """Decompose a free-text main task into a TaskPlan.

    For the showcase, the canonical Q2-tax string maps to `build_q2_tax_plan()`;
    anything else becomes a single-step plan routed to the data analyst as a
    safe default (read-only). A real implementation would run the orchestrator's
    own LLM (Qwen) to plan — that is wp-orchestrator's future extension; the
    deterministic path keeps the demo reproducible.
    """
    if "tax" in main_task.lower():
        plan = build_q2_tax_plan()
        plan.title = main_task
        return plan
    return TaskPlan(
        title=main_task,
        steps=[
            TaskStep(
                step_id="single",
                agent_id="data_analyst_agent_v1",
                intended_task=main_task,
            )
        ],
    )


_DECOMPOSE_SYSTEM = """\
You are the CFO Office orchestrator coordinating a team of specialist agents.
Given a GOAL and the TEAM (each agent has a role, an in-scope capability, and
limits), break the goal into an ordered list of concrete subtasks and assign
each subtask to exactly ONE agent by capability. Add dependencies where a step
needs an earlier step's output.

Return ONLY a JSON object of this shape (no markdown fences, no prose):

{
  "steps": [
    {"step_id": "snake_case_id", "agent_id": "<one of the team ids>",
     "intended_task": "one concrete imperative sentence",
     "depends_on": ["earlier_step_id"]}
  ]
}

Rules:
  - Use ONLY agent_ids that appear in the TEAM.
  - 3 to 6 steps; step_ids are unique snake_case.
  - intended_task is a single concrete instruction the assigned agent acts on.
  - Assign by best capability fit. Do NOT worry about permissions — a separate
    contract gate checks every delegation; your job is just a sensible plan.
"""


def decompose_llm(
    main_task: str,
    roster: list[dict],
    chat: Callable[[str, str], str],
) -> TaskPlan:
    """LLM-driven decomposition: plan + role assignment from the team roster.

    Calls the orchestrator's own LLM (e.g. Qwen) to turn a free-text goal into a
    multi-agent TaskPlan. Falls back to the deterministic ``decompose`` on any
    parse/validation failure, so a bad LLM output never hard-fails the run.
    """
    team = "\n".join(
        f"- {a['agent_id']}: {a.get('role', '')}. "
        f"in-scope: {a.get('scope', '')}; limits: {a.get('limits', '')}"
        for a in roster
    )
    user = f"GOAL:\n{main_task}\n\nTEAM:\n{team}\n\nProduce the plan as JSON."
    try:
        from charter.propose import _strip_markdown_fences

        data = json.loads(_strip_markdown_fences(chat(_DECOMPOSE_SYSTEM, user)))
        valid = {a["agent_id"] for a in roster}
        steps: list[TaskStep] = []
        seen: set[str] = set()
        for s in data.get("steps", []):
            aid, sid, task = s.get("agent_id"), s.get("step_id"), s.get("intended_task")
            if aid not in valid or not sid or not task or sid in seen:
                continue
            seen.add(sid)
            deps = [d for d in (s.get("depends_on") or []) if isinstance(d, str)]
            steps.append(TaskStep(step_id=sid, agent_id=aid, intended_task=task, depends_on=deps))
        # prune dangling deps (references to dropped steps) so they don't over-skip
        for st in steps:
            st.depends_on = [d for d in st.depends_on if d in seen]
        if steps:
            return TaskPlan(title=main_task, steps=steps)
    except Exception:
        pass
    return decompose(main_task)
