"""Seed the four CFO Office worker Charters from their profile.yaml files.

This is the `wp-charters` deliverable the orchestrator depends on. It is the
*deterministic* counterpart to `charter.projection.project` (which uses an LLM):
it loads each `examples/cfo_office/profiles/*.yaml`, projects it into typed
clauses with stable ids (`clauses.project_profile_to_clauses`), wraps them in a
fully-populated Charter, **signs** it with the principal's ed25519 key, and
**saves** it so a live charter server can serve it at
``{base_url}/{principal_id}/{agent_id}``.

Crucially, the raw profile text is NOT persisted — only its SHA-256 commitment
is written into `provenance.source_commitments[]`, exactly as the production
projector does. So the profile is genuine Principal Context: it drives the
Charter's clauses, and the Charter carries a tamper-evident pointer back to it,
without leaking the raw context into the public Charter.

`run_demo` imports `seed_cfo_office` and falls back to a tiny inline seed only if
this module is unavailable, so this is the normal path for both the keyless demo
and the live (qwen) run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from charter.projection import _hash_profile, load_profile
from charter.schema import (
    AgentOperator,
    Binding,
    Charter,
    Issuer,
    Lifecycle,
    Principal,
    Provenance,
    SourceCommitment,
    Summary,
)
from charter.signing import public_key_to_string, sign_charter
from charter.storage import ensure_issuer_key, save_charter

from .clauses import project_profile_to_clauses

# Profile filenames, in roster order (data analyst first — the read-only worker;
# comms last — it holds the external-send approval beat). Order only affects the
# returned dict's iteration order, which is what the LLM planner sees as the team
# roster, so we keep it stable and sensible.
_PROFILE_FILES = (
    "data_analyst_agent.yaml",
    "bookkeeping_agent.yaml",
    "tax_filing_agent.yaml",
    "comms_agent.yaml",
)

_PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


def _summary_for(profile_scope: list[str], agent_id: str) -> str:
    """A short plain-language Charter summary derived from the profile."""
    lead = profile_scope[0] if profile_scope else "scoped worker tasks"
    return (
        f"{agent_id}: {lead[0].lower() + lead[1:]} in scope. Out-of-scope actions "
        f"are incompatible (blocked); sensitive actions need principal approval."
    )


def _build_signed_charter(profile, profile_raw: str) -> Charter:
    """Project + sign one Charter from a validated Profile and its raw text."""
    now = datetime.now(UTC).replace(microsecond=0)
    principal_id = profile.principal.id
    agent_id = profile.agent.id

    private_key = ensure_issuer_key(principal_id)
    public_key_str = public_key_to_string(private_key.public_key())

    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(type="organization", id=principal_id, role_summary=profile.principal.role),
        issuer=Issuer(type="organization", id=principal_id, relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider", agent_card_url=profile.agent.card_url),
        summary=Summary(plain_language=_summary_for(profile.scope, agent_id)),
        clauses=project_profile_to_clauses(profile),
        lifecycle=Lifecycle(
            issued_at=now,
            valid_until=now + timedelta(days=profile.lifecycle.valid_days),
            status="active",
        ),
        provenance=Provenance(
            issuer_public_key=public_key_str,
            issuer_signature="",  # filled in by sign_charter below
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description=f"{principal_id}/{agent_id} profile, seeded {now.date().isoformat()}",
                    content_hash=_hash_profile(profile_raw),
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(charter, private_key)
    save_charter(charter)
    return charter


def seed_cfo_office(base_url: str = "http://localhost:8000") -> dict[str, Charter]:
    """Load the four profiles, project + sign + save their Charters, return them.

    Returns ``dict[agent_id -> Charter]`` in roster order. ``base_url`` is
    accepted for signature parity with the orchestrator's expectations; the
    binding URL is assembled by ``delegate_task`` from ``CHARTER_URL_BASE`` plus
    the binding's principal/agent ids, so it is not needed at seed time.
    """
    charters: dict[str, Charter] = {}
    for fname in _PROFILE_FILES:
        profile, raw = load_profile(_PROFILES_DIR / fname)
        charter = _build_signed_charter(profile, raw)
        charters[charter.binding.agent_id] = charter
    return charters


if __name__ == "__main__":
    seeded = seed_cfo_office()
    for aid, ch in seeded.items():
        types = ", ".join(sorted({c.type for c in ch.clauses}))
        print(f"{aid:>24}  {len(ch.clauses):>2} clauses  [{types}]")
