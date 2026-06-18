"""Deterministic profile -> clauses projection (no LLM).

The production projector (`charter.projection.project`) asks an LLM to expand a
profile.yaml into `clauses[]`. For the CFO Office *demo* we want the projection
to be **deterministic and key-free** so the reproducible (no-API) video take and
the protocol math are exercised identically every run. So this module projects
each profile field verbatim into a typed clause with a **stable, type-prefixed
id**:

    scope[i]            -> C-00i   (type=scope            -> allow)
    out_of_scope[i]     -> C-10i   (type=out_of_scope     -> incompatible)
    approval_required[i]-> C-20i   (type=approval_required-> needs_approval)
    data_handling.rules -> C-301   (type=data_handling    -> needs_approval)
    operational budget  -> C-401   (type=operational_limit-> needs_approval)
    style               -> C-501   (type=style            -> allow)

The clause `text` is copied verbatim from the profile bullet, so the profile is
the single source of truth: editing a bullet changes the gate behaviour, and the
profile's SHA-256 is committed into the Charter's provenance by the seeder.

The decision mapping itself lives in `charter.constants.TYPE_TO_DECISION` — this
module only assigns *which clause of which type* exists, never the decision.
"""

from __future__ import annotations

from charter.schema import Clause, Profile


def project_profile_to_clauses(profile: Profile) -> list[Clause]:
    """Project one validated Profile into an ordered, deterministic clause list.

    Pure and side-effect free. The ids are stable across runs so the demo's
    transparency log and any downstream assertions can refer to them.
    """
    clauses: list[Clause] = []

    for i, text in enumerate(profile.scope, start=1):
        clauses.append(Clause(id=f"C-{i:03d}", type="scope", text=text))

    for i, text in enumerate(profile.out_of_scope, start=1):
        clauses.append(Clause(id=f"C-1{i:02d}", type="out_of_scope", text=text))

    for i, text in enumerate(profile.approval_required, start=1):
        clauses.append(Clause(id=f"C-2{i:02d}", type="approval_required", text=text))

    if profile.data_handling and profile.data_handling.rules:
        clauses.append(
            Clause(id="C-301", type="data_handling", text=profile.data_handling.rules)
        )

    op = profile.operational
    if op and op.budget_per_task_usd is not None:
        parts = [f"Per-task budget {op.budget_per_task_usd:.2f} USD"]
        if op.budget_monthly_usd is not None:
            parts.append(f"monthly budget {op.budget_monthly_usd:.2f} USD")
        if op.hours and op.hours != "anytime":
            parts.append(f"operating hours {op.hours}")
        clauses.append(
            Clause(id="C-401", type="operational_limit", text="; ".join(parts) + ".")
        )

    if profile.style:
        clauses.append(Clause(id="C-501", type="style", text=profile.style))

    return clauses
