"""Cookbook #03 — Revoke without breaking in-flight tasks.

Walks through the calling-agent-side pattern that lets you revoke a
Charter without aborting tasks that are already mid-execution.

Plot:

    t0   issue a Charter, start a long-running task; cache the verdict
         on a per-task basis (the "in-flight ticket").
    t1   the principal decides to revoke (e.g. they discovered a
         policy violation). `charter revoke` flips lifecycle.status to
         "revoked" and re-signs.
    t2   a NEW fetch (e.g. for a different task) raises
         CharterRevokedError -> the calling agent refuses delegation.
    t3   the IN-FLIGHT task still has its cached verdict from t0, so
         it finishes correctly. After it finishes, the cached ticket
         is dropped — no new work can ride the old verdict.

The "grace period" is implicit: it is bounded by how long the
in-flight task takes, not by any wall-clock timer.

Run from the repo root:

    python examples/cookbook/03-revoke-without-breaking-inflight/main.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# In-flight ticket: a tiny calling-agent-side cache keyed by task_id.
# ---------------------------------------------------------------------------


@dataclass
class InFlightTicket:
    """A verdict snapshot bound to a single task_id.

    The whole point: once a task starts under a verdict, that verdict
    is THE source of truth for the task until it completes — no matter
    what happens to the Charter afterwards.
    """

    task_id: str
    charter_id: str
    verdict_decision: str  # "allow" / "needs_approval" / "incompatible"
    captured_at: datetime


class InFlightRegistry:
    """A trivial in-memory ticket store. Real systems would persist this
    in their job queue / workflow state so a worker restart can resume."""

    def __init__(self) -> None:
        self._tickets: dict[str, InFlightTicket] = {}

    def open(self, task_id: str, charter_id: str, decision: str) -> InFlightTicket:
        t = InFlightTicket(
            task_id=task_id,
            charter_id=charter_id,
            verdict_decision=decision,
            captured_at=datetime.now(UTC),
        )
        self._tickets[task_id] = t
        return t

    def get(self, task_id: str) -> InFlightTicket | None:
        return self._tickets.get(task_id)

    def close(self, task_id: str) -> None:
        self._tickets.pop(task_id, None)


# ---------------------------------------------------------------------------
# Build a Charter and place it on disk under a scratch CHARTER_DATA_DIR
# ---------------------------------------------------------------------------


def _build_and_save_charter() -> tuple[str, str]:
    from charter.schema import (
        AgentOperator,
        Binding,
        Charter,
        Clause,
        Issuer,
        Lifecycle,
        Principal,
        Provenance,
        Summary,
    )
    from charter.signing import public_key_to_string, sign_charter
    from charter.storage import ensure_issuer_key, save_charter

    now = datetime.now(UTC).replace(microsecond=0)
    valid_until = now + timedelta(days=30)
    private_key = ensure_issuer_key("alice@acme.com")
    pub = public_key_to_string(private_key.public_key())

    charter = Charter(
        charter_id=f"charter:alice@acme.com:research_agent_v1:{now.date().isoformat()}",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Senior Accountant"),
        issuer=Issuer(id="alice@acme.com", relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(plain_language="Accounting work allowed; marketing forbidden."),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting and tax work"),
            Clause(id="C-101", type="out_of_scope", text="Marketing copy"),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=valid_until, status="active"),
        provenance=Provenance(
            issuer_public_key=pub,
            issuer_signature="",
            generated_at=now,
        ),
    )
    sign_charter(charter, private_key)
    save_charter(charter)
    return charter.charter_id, charter.binding.agent_id


# ---------------------------------------------------------------------------
# The two calling-agent entry points
# ---------------------------------------------------------------------------


def begin_task(
    registry: InFlightRegistry,
    *,
    principal_id: str,
    agent_id: str,
    task_id: str,
    task_description: str,
) -> InFlightTicket | str:
    """Returns a ticket on success, or a human-readable refusal string."""
    from charter.errors import CharterError
    from charter.storage import load_charter

    # In a real system you'd use fetch_charter over HTTP. For an offline
    # cookbook we read from disk directly via load_charter — same code
    # path that the FastAPI route uses to serve /<p>/<a>.
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        return f"refused: no Charter for {principal_id}/{agent_id}"

    # Replicate the lifecycle-status check that _fetch_and_verify does.
    if charter.lifecycle.status == "revoked":
        # Emulate what CharterRevokedError would look like to the caller.
        raise _make_revoked(charter.charter_id)

    # Caller's grader normally runs here. We hard-code a verdict that
    # this task is in-scope.
    decision = "allow"
    print(f"  [verdict] task={task_id!r} decision={decision}")
    return registry.open(task_id, charter.charter_id, decision)


def _make_revoked(charter_id: str) -> Exception:
    from charter.errors import CharterRevokedError

    return CharterRevokedError(f"Charter {charter_id} is revoked")


def complete_task(registry: InFlightRegistry, *, task_id: str) -> str:
    """Finish a task that holds an in-flight ticket. The ticket's
    cached verdict is what gates whether work actually runs.

    Real production code would re-check the verdict immediately before
    the irreversible action (e.g. before the actual `stripe.Charge.create`
    call) for tasks whose execution is long enough to make a stale
    verdict risky. This example shows the simple "trust the ticket"
    pattern; for the strict variant, see the pitfalls section in the
    cookbook markdown.
    """
    ticket = registry.get(task_id)
    if ticket is None:
        return f"refused: no in-flight ticket for {task_id}"
    if ticket.verdict_decision != "allow":
        registry.close(task_id)
        return (
            f"refused: in-flight ticket for {task_id} had decision="
            f"{ticket.verdict_decision} (not allow)"
        )
    print(f"  [execute] task={task_id!r} using cached verdict from t0")
    registry.close(task_id)
    return f"ok: task {task_id} completed under verdict captured at {ticket.captured_at.isoformat()}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Use a scratch data dir so this cookbook can't trash your real charters.
    scratch = _ROOT / "data" / "cookbook_03"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)

    print("=" * 72)
    print("Cookbook #03 — Revoke without breaking in-flight tasks")
    print("=" * 72)
    print()

    # ---- t0: issue + start an in-flight task ------------------------------
    charter_id, agent_id = _build_and_save_charter()
    principal_id = "alice@acme.com"
    print(f"t0  Charter issued: {charter_id}")

    registry = InFlightRegistry()
    ticket_a = begin_task(
        registry,
        principal_id=principal_id,
        agent_id=agent_id,
        task_id="task_001",
        task_description="Reconcile Q1 invoices (multi-minute run).",
    )
    assert isinstance(ticket_a, InFlightTicket), f"unexpected refusal: {ticket_a}"
    print(f"     in-flight ticket opened: task_id={ticket_a.task_id}")
    print()

    # ---- t1: revoke the Charter ------------------------------------------
    time.sleep(0.01)  # so t0 and t1 timestamps differ visibly
    _revoke_charter_inplace(principal_id, agent_id)
    print(f"t1  Charter revoked: {charter_id}")
    print()

    # ---- t2: a NEW task starts. Fetch raises CharterRevokedError. --------
    from charter.errors import CharterRevokedError

    try:
        _ = begin_task(
            registry,
            principal_id=principal_id,
            agent_id=agent_id,
            task_id="task_002",
            task_description="Another reconciliation pass.",
        )
        print("t2  unexpected: new task got a ticket")
        return 1
    except CharterRevokedError as e:
        print(f"t2  new task refused: {type(e).__name__}: {e}")
    print()

    # ---- t3: in-flight task finishes using its cached verdict ------------
    result = complete_task(registry, task_id="task_001")
    print(f"t3  in-flight {result}")
    assert result.startswith("ok: task task_001 completed")
    print()

    print("[OK] Revocation refused the NEW task but did not abort the in-flight one.")
    print(f"     Scratch data dir: {scratch}")
    return 0


def _revoke_charter_inplace(principal_id: str, agent_id: str) -> None:
    """Replicate `charter revoke` (cli.py) without going through Click."""
    from charter.signing import sign_charter
    from charter.storage import ensure_issuer_key, load_charter, save_charter

    charter = load_charter(principal_id, agent_id)
    assert charter is not None
    charter.lifecycle.status = "revoked"
    charter.lifecycle.revoked_at = datetime.now(UTC).replace(microsecond=0)
    charter.provenance.issuer_signature = ""
    sign_charter(charter, ensure_issuer_key(principal_id))
    save_charter(charter)


if __name__ == "__main__":
    sys.exit(main())
