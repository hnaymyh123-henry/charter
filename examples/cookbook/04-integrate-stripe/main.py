"""Cookbook #04 — Charter-gated Stripe payment.

Pattern: every call into a third-party "money-moving" SDK should be
preceded by a Charter preflight. This example wires that pattern
around a fake Stripe SDK so you can run it offline; the production
shape is identical, only the import line and the Charge.create signature
differ.

The flow inside `make_payment`:

    1. preflight: load Charter, ask the calling LLM (here: a hand-coded
       grader) which clauses are hit, run aggregate_verdict.
    2. on decision != "allow": refuse the payment, return the verdict.
    3. on decision == "allow": call Stripe (the fake), record the
       charge id alongside the verdict.

We exercise two tasks:

    - "Refund a customer for $30 (in-scope refund task)"     -> allow
    - "Wire transfer $50,000 to a new vendor (PII + high-$)"  -> needs_approval

Run from the repo root:

    python examples/cookbook/04-integrate-stripe/main.py
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Tiny fake Stripe SDK. Real shape mirrors stripe.Charge.create(...).
# ---------------------------------------------------------------------------


class FakeStripeError(RuntimeError):
    pass


class FakeStripe:
    """Stand-in for `import stripe`. Records calls so the cookbook can
    prove the gated path did NOT reach the SDK."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def charge_create(
        self, *, amount_cents: int, currency: str, description: str, customer_id: str
    ) -> dict[str, Any]:
        # Real Stripe would talk to the network here.
        ch_id = f"ch_{len(self.calls) + 1:06d}"
        record = {
            "id": ch_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "description": description,
            "customer_id": customer_id,
            "status": "succeeded",
        }
        self.calls.append(record)
        return record


# ---------------------------------------------------------------------------
# Build a Charter that has the clauses we want to hit.
# ---------------------------------------------------------------------------


def _build_charter() -> tuple[str, str]:
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
    private_key = ensure_issuer_key("ops@acme.com")
    charter = Charter(
        charter_id=f"charter:ops@acme.com:billing_agent_v1:{now.date().isoformat()}",
        binding=Binding(principal_id="ops@acme.com", agent_id="billing_agent_v1"),
        principal=Principal(id="ops@acme.com", role_summary="Acme Billing Ops"),
        issuer=Issuer(id="ops@acme.com", relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(
            plain_language=(
                "Billing agent may process refunds and small recurring charges; "
                "wire transfers and any payment over USD 10,000 require approval."
            )
        ),
        clauses=[
            Clause(
                id="C-001",
                type="scope",
                text="Routine billing operations: refunds, recurring subscription charges under USD 1,000.",
            ),
            Clause(
                id="C-002",
                type="approval_required",
                text="Any wire transfer to a new vendor or payment exceeding USD 10,000 requires explicit principal approval.",
            ),
            Clause(
                id="C-003",
                type="approval_required",
                text="Any handling of customer personally identifiable information requires explicit principal approval.",
            ),
        ],
        lifecycle=Lifecycle(
            issued_at=now, valid_until=now + timedelta(days=30), status="active"
        ),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(private_key.public_key()),
            issuer_signature="",
            generated_at=now,
        ),
    )
    sign_charter(charter, private_key)
    save_charter(charter)
    return charter.binding.principal_id, charter.binding.agent_id


# ---------------------------------------------------------------------------
# Hand-coded grader (in production this is your LLM call).
# ---------------------------------------------------------------------------


def grade_task(charter: Any, task: str) -> list[dict[str, Any]]:
    """Inspect the task string and return per-clause hits.

    A production grader replaces this with one Anthropic or OpenAI
    Chat Completions call using the GRADE_SYSTEM prompt in
    charter/prompts.py. The output shape is the same.
    """
    t = task.lower()
    hits: list[dict[str, Any]] = []
    if "refund" in t and "$" in t:
        hits.append(
            {
                "id": "C-001",
                "hit": True,
                "confidence": 0.92,
                "reason": "Routine refund matches billing scope.",
            }
        )
    if "wire transfer" in t or "new vendor" in t or "$50,000" in t:
        hits.append(
            {
                "id": "C-002",
                "hit": True,
                "confidence": 0.95,
                "reason": "Wire transfer / over USD 10,000 trigger approval.",
            }
        )
    if "ssn" in t or "social security" in t or "pii" in t:
        hits.append(
            {
                "id": "C-003",
                "hit": True,
                "confidence": 0.93,
                "reason": "Task references customer PII.",
            }
        )
    return hits


# ---------------------------------------------------------------------------
# The actual Charter-gated wrapper around Stripe.
# ---------------------------------------------------------------------------


def make_payment(
    *,
    stripe: FakeStripe,
    principal_id: str,
    agent_id: str,
    task_description: str,
    amount_cents: int,
    currency: str,
    customer_id: str,
) -> dict[str, Any]:
    """Charter-gated payment. Returns either {"ok": True, "charge": ...}
    or {"ok": False, "verdict": ..., "skipped": True}."""
    from charter.mcp_server import aggregate_verdict
    from charter.storage import load_charter

    charter = load_charter(principal_id, agent_id)
    if charter is None:
        return {"ok": False, "skipped": True, "reason": "no Charter"}

    hits = grade_task(charter, task_description)
    fn = getattr(aggregate_verdict, "fn", aggregate_verdict)
    verdict = fn(charter.model_dump(mode="json"), hits)

    if verdict["decision"] != "allow":
        return {"ok": False, "skipped": True, "verdict": verdict}

    charge = stripe.charge_create(
        amount_cents=amount_cents,
        currency=currency,
        description=task_description,
        customer_id=customer_id,
    )
    return {"ok": True, "charge": charge, "verdict": verdict}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    scratch = _ROOT / "data" / "cookbook_04"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)

    print("=" * 72)
    print("Cookbook #04 — Charter-gated Stripe payment")
    print("=" * 72)
    print()

    principal_id, agent_id = _build_charter()
    stripe = FakeStripe()

    cases = [
        {
            "task_description": "Issue a $30 refund to customer cus_OK on a routine return.",
            "amount_cents": 3000,
            "currency": "usd",
            "customer_id": "cus_OK",
        },
        {
            "task_description": "Wire transfer $50,000 to a new vendor account.",
            "amount_cents": 5_000_000,
            "currency": "usd",
            "customer_id": "cus_NEW_VENDOR",
        },
    ]

    expected = ["allow", "needs_approval"]
    for case, want in zip(cases, expected, strict=True):
        print(f"--- task: {case['task_description']!r}")
        result = make_payment(
            stripe=stripe,
            principal_id=principal_id,
            agent_id=agent_id,
            **case,
        )
        if result["ok"]:
            decision = result["verdict"]["decision"]
            charge_id = result["charge"]["id"]
            print(f"    verdict={decision}  charge_id={charge_id}")
            assert want == "allow", f"expected refusal for {want!r}, got allow"
        else:
            decision = result.get("verdict", {}).get("decision", "unknown")
            applied = [
                m["id"]
                for m in result.get("verdict", {}).get("matched_clauses", [])
                if m["applied"]
            ]
            print(f"    REFUSED  verdict={decision}  applied={applied}")
            assert decision == want, f"expected {want!r}, got {decision!r}"
        print()

    print(f"Stripe SDK was called {len(stripe.calls)} time(s):")
    for c in stripe.calls:
        print(f"    {c['id']}  amount_cents={c['amount_cents']}  desc={c['description']!r}")
    assert len(stripe.calls) == 1, "Only the refund should have hit Stripe."
    print()
    print("[OK] Charter gate let the allowed payment through and held back the rest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
