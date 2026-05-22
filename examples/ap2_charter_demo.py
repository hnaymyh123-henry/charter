"""End-to-end AP2 + Charter integration demo.

Scenario: a pay-by-agent assistant tries to send a one-shot $200
transfer on behalf of Alice. Alice's Charter authorizes the assistant
to spend autonomously up to $500 per transaction; anything over needs
her explicit approval.

This script does NOT touch a real AP2 SDK or a real payment rail. It
constructs an in-memory mandate dict matching the shape documented in
`charter.adapters.ap2`, builds an in-memory Charter (no network, no
LLM), and runs `verify()` with a stub grader that judges hits the way
a calling agent's own LLM would. The point is to show the verifier's
control flow end-to-end and what `AP2VerifyResult` looks like in each
of the three terminal states.

Run::

    python examples/ap2_charter_demo.py

Expected output (truncated):

    Scenario 1: $200 transfer (within Charter spending cap)
      final_decision = allow
    Scenario 2: $1500 transfer (over Charter spending cap)
      final_decision = needs_approval
    Scenario 3: mandate signature missing (tampered envelope)
      final_decision = incompatible
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from charter.adapters.ap2 import embed_charter_in_mandate, verify  # noqa: E402
from charter.schema import (  # noqa: E402
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    Provenance,
    SourceCommitment,
    Summary,
)
from charter.signing import generate_keypair, public_key_to_string, sign_charter  # noqa: E402

CHARTER_URL = "https://example.test/alice@acme.com/pay_agent_v1"


def build_demo_charter() -> Charter:
    """Hand-roll Alice's pay-agent Charter.

    Two clauses model the real spending policy:
      - C-001 (scope):              autonomous low-value transfers (<= $500)
      - C-002 (approval_required):  any transfer above $500
    """
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:alice@acme.com:pay_agent_v1:demo",
        binding=Binding(principal_id="alice@acme.com", agent_id="pay_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Personal finance assistant user."),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic-pay-agent"),
        summary=Summary(
            plain_language=(
                "Pay agent may autonomously send transfers up to USD 500 per "
                "transaction; larger transfers require Alice's approval."
            )
        ),
        clauses=[
            Clause(
                id="C-001",
                type="scope",
                text="Send one-shot USD transfers up to and including USD 500 without further approval.",
            ),
            Clause(
                id="C-002",
                type="approval_required",
                text="Any single transfer larger than USD 500 requires explicit approval from the principal.",
            ),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="demo profile",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(charter, private)
    return charter


def stub_grader(charter: Charter, task: str) -> list[dict[str, Any]]:
    """Stand-in for the calling agent's LLM grader.

    Inspects the task text for an explicit USD amount and returns
    deterministic hits accordingly. Real callers would hand the Charter
    + task to their LLM with the standard `GRADE_SYSTEM` prompt.
    """
    text = task.lower()
    # Pull the first integer-or-float USD amount out of the task. Simple
    # heuristic — fine for a fixture, not a parser.
    amount = 0.0
    for tok in text.replace("$", " ").replace(",", " ").split():
        try:
            amount = float(tok.rstrip("."))
            break
        except ValueError:
            continue

    hits: list[dict[str, Any]] = []
    if amount <= 500.0:
        hits.append(
            {
                "id": "C-001",
                "hit": True,
                "confidence": 0.95,
                "reason": f"Amount {amount} USD is within the autonomous cap of 500.",
            }
        )
    else:
        hits.append(
            {
                "id": "C-002",
                "hit": True,
                "confidence": 0.95,
                "reason": f"Amount {amount} USD exceeds 500; requires approval.",
            }
        )
    return hits


def build_mandate(amount: float, *, signature: str = "ap2-sig-mock") -> dict[str, Any]:
    """Build a minimal AP2 mandate dict and embed Alice's charter_url."""
    mandate = {
        "payer": "alice@acme.com",
        "payee": "merchant@example.com",
        "amount": {"value": amount, "currency": "USD"},
        "task": f"Pay ${amount} to merchant for a one-shot purchase.",
        "signature": signature,
    }
    return embed_charter_in_mandate(mandate, CHARTER_URL)


def main() -> int:
    charter = build_demo_charter()

    def fetch(_url: str) -> Charter:
        return charter

    scenarios = [
        ("Scenario 1: $200 transfer (within Charter spending cap)", build_mandate(200)),
        ("Scenario 2: $1500 transfer (over Charter spending cap)", build_mandate(1500)),
        (
            "Scenario 3: mandate signature missing (tampered envelope)",
            build_mandate(200, signature=""),
        ),
    ]

    for headline, mandate in scenarios:
        result = verify(mandate, fetch_charter_fn=fetch, hits_grader=stub_grader)
        print(headline)
        print(f"  mandate_ok      = {result.mandate_ok}")
        print(
            "  charter_verdict = "
            f"{result.charter_verdict.decision if result.charter_verdict else None}"
        )
        print(f"  final_decision  = {result.final_decision}")
        print(f"  reason          = {result.reason}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
