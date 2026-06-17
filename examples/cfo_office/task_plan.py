"""The canonical Q2-tax task DAG — wp-orchestrator deliverable.

Decomposes the main task "Complete Q2 tax filing, then notify the external
auditor" into ordered TaskSteps, each routed to one worker agent_id. The DAG is
the single canonical main task the demo and experiment run against.

Each TaskStep carries the additive envelope fields the base mcp_server ignores
but the orchestrator/workers read (step_id, depends_on, context). Keeping this
hand-authored (no LLM) makes the demo deterministic for the <3-min video.
"""

from __future__ import annotations

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
