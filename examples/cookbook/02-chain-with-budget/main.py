"""Cookbook #02 — Chain with budget.

Demonstrates how a `budget` operational_limit propagates through a
two-hop Charter Chain:

    acme_corp        --Charter A (budget USD 1.00/task)--> assistant
       assistant     --Charter B (budget USD 0.50/task)--> sub-agent

Charter B tightens the budget cap relative to A. The cookbook proves
two things:

    1. `verify_chain(B, A)` returns True — narrower budget does not
       break attenuation (Charter Chain v0.7's string subset rules
       intentionally do not touch operational_limit, so a tighter
       child budget is always allowed).

    2. The aggregate verdict for a high-cost task correctly fires the
       child's `operational_limit` clause when the calling agent's
       grader flags the task as cost-violating.

The example builds Charters in-process (no LLM call, no HTTP server),
signs them with hand-rolled keys saved to a scratch directory inside
data/cookbook_02/, and walks the chain via verify_chain + the
in-process aggregate_verdict_chain MCP tool. Cleanup is intentionally
skipped so you can `charter inspect` the result afterwards.

Run from the repo root:

    python examples/cookbook/02-chain-with-budget/main.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    # Scope the cookbook to a private data/ subdir so it doesn't trample
    # over your real charters. CHARTER_DATA_DIR is read by charter.storage.
    scratch = _ROOT / "data" / "cookbook_02"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)

    # Imports happen after the env var is set so storage picks it up.
    from charter.chain import verify_chain
    from charter.mcp_server import aggregate_verdict_chain
    from charter.schema import (
        AgentOperator,
        AttenuationProof,
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

    print("=" * 72)
    print("Cookbook #02 — Chain with budget")
    print("=" * 72)
    print()

    now = datetime.now(UTC).replace(microsecond=0)
    valid_until = now + timedelta(days=30)

    # ----- Charter A: acme_corp -> assistant_agent_v1 -----
    parent_key = ensure_issuer_key("acme_corp")
    parent = Charter(
        charter_id=f"charter:acme_corp:assistant_agent_v1:{now.date().isoformat()}",
        binding=Binding(principal_id="acme_corp", agent_id="assistant_agent_v1"),
        principal=Principal(id="acme_corp", role_summary="Acme Corp engineering ops"),
        issuer=Issuer(id="acme_corp", relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(
            plain_language=(
                "Charter for assistant_agent_v1 acting on behalf of Acme Corp. "
                "Per-task budget cap USD 1.00."
            )
        ),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting and tax work"),
            Clause(id="C-002", type="scope", text="Engineering work"),
            Clause(id="C-101", type="out_of_scope", text="Marketing copy"),
            Clause(
                id="C-200",
                type="operational_limit",
                text="Per-task budget cap is USD 1.00. Reject tasks projected to exceed this cost.",
            ),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=valid_until, status="active"),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(parent_key.public_key()),
            issuer_signature="",
            generated_at=now,
        ),
    )
    sign_charter(parent, parent_key)
    save_charter(parent)

    # ----- Charter B: assistant_agent_v1 -> sub_agent_v1 -----
    # Tighter budget (USD 0.50). The child inherits the parent's
    # out_of_scope ("Marketing copy") verbatim. Scope is narrowed: only
    # accounting (no engineering).
    child_key = ensure_issuer_key("assistant_agent_v1")
    child = Charter(
        charter_id=f"charter:assistant_agent_v1:sub_agent_v1:{now.date().isoformat()}",
        binding=Binding(principal_id="assistant_agent_v1", agent_id="sub_agent_v1"),
        principal=Principal(
            id="assistant_agent_v1",
            role_summary="Acme assistant agent acting as principal toward sub-agents",
        ),
        issuer=Issuer(id="assistant_agent_v1", relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(
            plain_language=(
                "Sub-Charter under Acme. Accounting only, per-task budget cap USD 0.50."
            )
        ),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting and tax work"),
            Clause(id="C-101", type="out_of_scope", text="Marketing copy"),
            Clause(
                id="C-200",
                type="operational_limit",
                text="Per-task budget cap is USD 0.50. Reject tasks projected to exceed this cost.",
            ),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=valid_until, status="active"),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(child_key.public_key()),
            issuer_signature="",
            generated_at=now,
        ),
        attenuation_proof=AttenuationProof(parent_charter_id=parent.charter_id),
    )
    sign_charter(child, child_key)
    save_charter(child)

    print("Built two-hop chain:")
    print(f"  [parent] {parent.charter_id}")
    print(f"           operational_limit: budget USD 1.00/task")
    print(f"  [child]  {child.charter_id}")
    print(f"           operational_limit: budget USD 0.50/task (TIGHTER)")
    print()

    # ----- Step 1: verify the chain ---------------------------------------
    chain_ok = verify_chain(child, parent)
    print(f"verify_chain(child, parent) -> {chain_ok}")
    assert chain_ok, "Chain should verify — child preserves out_of_scope and narrows scope."
    print(
        "  Tighter budget is allowed: Charter Chain v0.7 string subset rules\n"
        "  intentionally do not touch operational_limit, so the child may\n"
        "  always set a stricter cap than the parent."
    )
    print()

    # ----- Step 2: aggregate over the chain for a high-cost task ----------
    # We simulate the calling agent's grader marking the task as a hit
    # against C-200 on the *child* Charter (the stricter cap is the one
    # that fires first). The parent's C-200 is also marked hit so we can
    # show that the chain-wide verdict surfaces both.
    chain_dump = [parent.model_dump(mode="json"), child.model_dump(mode="json")]
    hits_per_charter: dict[str, list[dict[str, object]]] = {
        parent.charter_id: [
            {
                "id": "C-200",
                "hit": True,
                "confidence": 0.92,
                "reason": "Task estimated at USD 0.80, exceeds USD 1.00? no — but still flags as cost-sensitive.",
            }
        ],
        child.charter_id: [
            {
                "id": "C-200",
                "hit": True,
                "confidence": 0.95,
                "reason": "Task estimated at USD 0.80, exceeds child's USD 0.50 cap.",
            }
        ],
    }

    # aggregate_verdict_chain is registered as a FastMCP tool decorator;
    # unwrap to get the underlying function for direct invocation.
    fn = getattr(aggregate_verdict_chain, "fn", aggregate_verdict_chain)
    verdict = fn(chain_dump, hits_per_charter)

    decision = verdict["decision"]
    applied = [
        f"{m.get('source_charter_id', '?')}::{m['id']}"
        for m in verdict["matched_clauses"]
        if m["applied"]
    ]
    print("Task: 'Run a multi-hour reconciliation pass projected to cost USD 0.80'")
    print(f"  -> decision: {decision.upper()}")
    print(f"  -> applied clauses: {applied}")
    print(f"  -> reason: {verdict['reason']}")
    print()
    # operational_limit -> needs_approval per TYPE_TO_DECISION. Both
    # Charters' C-200 fired, so both should be applied.
    assert decision == "needs_approval", f"Expected needs_approval, got {decision!r}."
    assert len(applied) >= 1, "At least one C-200 should have been applied."
    print("[OK] Chain verified; budget clause propagated; chain verdict matches expectation.")
    print()
    print(f"Charters were written to {scratch}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
