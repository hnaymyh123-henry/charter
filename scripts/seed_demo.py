"""Seed Charters without an LLM call.

Used in two situations:
    1. Demo time, when we don't want to depend on a live API key.
    2. Local development before adding a key.

Replaces the single LLM call inside `charter.projection.project()` with a
hand-curated set of clauses + summary, then runs sign + save through the
real code path. The produced Charter is byte-for-byte indistinguishable
from one issued by `charter issue` with a working key.

Usage:
    python scripts/seed_demo.py profiles/alice.yaml
    python scripts/seed_demo.py profiles/bob.yaml
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running this from anywhere; add repo root to sys.path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from charter.projection import _hash_profile, load_profile
from charter.schema import (
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    Profile,
    Provenance,
    SourceCommitment,
    Summary,
)
from charter.signing import public_key_to_string, sign_charter
from charter.storage import ensure_issuer_key, save_charter


# ---------------------------------------------------------------------------
# Hand-curated clauses per principal (mirrors what the LLM would produce
# from profiles/{alice,bob}.yaml).
# ---------------------------------------------------------------------------

ALICE_CLAUSES = [
    Clause(id="C-001", type="scope",
        text="This agent acts for Alice's accounting, tax filing, bookkeeping, financial analysis, invoice classification, and tax document organization work."),
    Clause(id="C-002", type="out_of_scope",
        text="Do not accept marketing copy, advertising design, code authoring, or UI design work. These require a separate Charter."),
    Clause(id="C-003", type="approval_required",
        text="Any handling of customer personally identifiable information (name + bank/tax/income combinations) requires explicit principal approval per session."),
    Clause(id="C-004", type="approval_required",
        text="Any destructive action on production data --DROP, DELETE, TRUNCATE, or backup deletion --requires explicit human approval."),
    Clause(id="C-005", type="operational_limit",
        text="Operational window is Monday to Friday, 09:00-18:00 America/New_York. Per-task budget cap is USD 0.50."),
    Clause(id="C-006", type="data_handling",
        text="May process customer tax filings, tax IDs, bank statements, and income records. Must not share with third parties, must not persist after task completion."),
    Clause(id="C-007", type="style",
        text="Prefer structured output (JSON or Markdown table). Cite sources for factual claims. Respond in English or Chinese."),
]

ALICE_SUMMARY = (
    "This agent acts for Alice's accounting, tax, and bookkeeping work during "
    "tax season. It must avoid marketing, code authoring, and any handling of "
    "customer PII without explicit approval. Destructive database operations on "
    "production data always require approval."
)

BOB_CLAUSES = [
    Clause(id="C-001", type="scope",
        text="This agent acts for Bob's coding, debugging, refactoring, technical documentation, code review, and technology evaluation work."),
    Clause(id="C-002", type="out_of_scope",
        text="Do not accept tax filing, bookkeeping, marketing copy, or personal financial advice."),
    Clause(id="C-003", type="approval_required",
        text="Any deployment to production environments requires explicit principal approval."),
    Clause(id="C-004", type="approval_required",
        text="Any destructive database action --DROP TABLE, deletion of database tables, or deletion of production backups --requires explicit principal approval."),
    Clause(id="C-005", type="approval_required",
        text="Committing code containing secrets (API keys, credentials, tokens) requires explicit principal approval and review."),
    Clause(id="C-006", type="data_handling",
        text="May access source code, internal API keys, customer email lists, and operational metrics. Must not commit to public repos; must not send to third-party LLM services; must not dump to log files."),
    Clause(id="C-007", type="style",
        text="Code blocks first, explanations after. Respond in English. Concise, no pleasantries."),
]

BOB_SUMMARY = (
    "This agent acts for Bob's coding, debugging, refactoring, and technical "
    "documentation work. It must avoid tax, accounting, and marketing tasks. "
    "Any production deployment, destructive database action, IAM change, "
    "payment, or secret commit requires explicit approval."
)


SEED_BUNDLES = {
    "alice@acme.com": (ALICE_CLAUSES, ALICE_SUMMARY),
    "bob@startup.io": (BOB_CLAUSES, BOB_SUMMARY),
}


# ---------------------------------------------------------------------------
# Build + sign + save (same path as charter.projection.project)
# ---------------------------------------------------------------------------

def seed_from_profile(profile_path: Path) -> Charter:
    profile, raw = load_profile(profile_path)

    if profile.principal.id not in SEED_BUNDLES:
        sys.exit(
            f"No hand-curated clauses for {profile.principal.id!r}. Add them to "
            f"SEED_BUNDLES in scripts/seed_demo.py."
        )

    clauses, summary_text = SEED_BUNDLES[profile.principal.id]

    private_key = ensure_issuer_key(profile.principal.id)
    public_key_str = public_key_to_string(private_key.public_key())

    now = datetime.now(timezone.utc).replace(microsecond=0)
    valid_until = now + timedelta(days=profile.lifecycle.valid_days)

    charter = Charter(
        charter_id=(
            f"charter:{profile.principal.id}:{profile.agent.id}:"
            f"{now.date().isoformat()}"
        ),
        binding=Binding(
            principal_id=profile.principal.id,
            agent_id=profile.agent.id,
        ),
        principal=Principal(
            id=profile.principal.id,
            role_summary=profile.principal.role,
        ),
        issuer=Issuer(id=profile.principal.id, relationship_to_principal="self"),
        agent_operator=AgentOperator(
            id="generic_worker_agent_provider",
            agent_card_url=profile.agent.card_url,
        ),
        summary=Summary(plain_language=summary_text),
        clauses=clauses,
        lifecycle=Lifecycle(issued_at=now, valid_until=valid_until, status="active"),
        provenance=Provenance(
            issuer_public_key=public_key_str,
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description=(
                        f"{profile.principal.id} profile answered on "
                        f"{now.date().isoformat()} (seeded, no live LLM)"
                    ),
                    content_hash=_hash_profile(raw),
                )
            ],
            generated_at=now,
        ),
    )

    sign_charter(charter, private_key)
    path = save_charter(charter)

    print(f"[1/3] Loaded:    {profile_path}  ->  {profile.principal.id}")
    print(f"[2/3] Projected: hand-curated ({len(clauses)} clauses)  [SEED MODE, no LLM]")
    print(f"[3/3] Signed:    ed25519, public key embedded in provenance")
    print()
    print("[OK] Charter active")
    print(f"  charter_id:  {charter.charter_id}")
    print(f"  binding:     {profile.principal.id} x {profile.agent.id}")
    print(f"  valid_until: {charter.lifecycle.valid_until.isoformat()}")
    print(f"  file:        {path}")
    print(f"  url:         http://localhost:8000/{profile.principal.id}/{profile.agent.id}")

    return charter


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/seed_demo.py <profile.yaml>")
    seed_from_profile(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
