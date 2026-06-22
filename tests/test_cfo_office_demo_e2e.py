"""End-to-end smoke test for the CFO Office demo (keyless, deterministic).

This guards the *wiring* of the whole orchestrator path — decompose/route/gate/
escalate/grant/execute — which unit tests on `workers.worker` alone do NOT cover.
It exists specifically because the empty-task envelope bug (delegate_task does not
echo the task text back, so the worker graded an empty action and every step fell
to the closed-world `needs_approval` default) was invisible to per-worker tests
and only showed up when running the full `run_demo` path.

Runs fully keyless: the deterministic stub grader, a real in-process charter
server (so the step-up tools' HTTP re-fetch + signature-verify actually execute),
and the demo approval policy. No API key, no network egress.
"""

from __future__ import annotations

import os

from examples.cfo_office.charters.seed_cfo_office import seed_cfo_office
from examples.cfo_office.orchestrator import (
    CFOOrchestrator,
    _live_server,
    _make_demo_approval_cb,
    _stub_grader,
)
from examples.cfo_office.task_plan import build_q2_tax_plan


def test_keyless_q2_demo_timeline(tmp_path, monkeypatch):
    """The canonical Q2 plan must converge to the intended governed timeline."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))

    with _live_server() as base_url:
        os.environ["CHARTER_URL_BASE"] = base_url
        charters = seed_cfo_office(base_url=base_url)
        approval_cb = _make_demo_approval_cb(charters)
        orch = CFOOrchestrator(
            charters, grader=_stub_grader, base_url=base_url, approval_cb=approval_cb
        )
        result = orch.run(build_q2_tax_plan())

    by = {s.step_id: s for s in result.steps}

    # 1) Routine steps are in scope -> allow -> executed (NOT silently gated).
    for sid in ("pull_ledger", "reconcile_invoices", "compute_tax"):
        assert by[sid].effective_decision == "allow", (sid, by[sid].effective_decision)
        assert by[sid].executed, f"{sid} should have executed"

    # 2) The destructive DROP is caught and HELD — gated to needs_approval and,
    #    under the demo policy, denied (not auto-run). The red-line/pause beat.
    drop = by["tax_drop_temp"]
    assert drop.effective_decision == "needs_approval"
    assert not drop.executed
    assert drop.skipped

    # 3) The external-auditor send escalates -> a real one-shot AdHocGrant -> executed.
    notify = by["notify_auditor"]
    assert notify.executed, "external send should execute under a grant"
    assert notify.grant_id, "execution must carry a grant_id"

    # 4) Exactly one grant was minted (the auditor send), and the run is partial
    #    (4 executed, the DROP held).
    assert len(result.escalations) == 1
    assert result.final_status == "partial"
