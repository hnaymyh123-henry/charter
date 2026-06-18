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

The clause `text` is copied verbatim from the profile bullet, so the profile is
the single source of truth: editing a bullet changes the gate behaviour, and the
profile's SHA-256 is committed into the Charter's provenance by the seeder.

The decision mapping itself lives in `charter.constants.TYPE_TO_DECISION` — this
module only assigns *which clause of which type* exists, never the decision.

Why only three of the six clause types reach the gate
-----------------------------------------------------
The gate runs on EVERY delegation, so a clause type only belongs here if it is a
genuine *per-action* decision. We project the three decision-bearing types:

    scope (allow) · out_of_scope (incompatible) · approval_required (needs_approval)

We deliberately do NOT project the profile's `data_handling`, `operational`
(budget/hours), or `style` fields into gate clauses. Those are *standing
obligations* the agent must honour continuously, not triggers to re-gate on each
step — and because `TYPE_TO_DECISION` maps `data_handling` and
`operational_limit` to `needs_approval`, a semantic grader (which reasonably
judges that almost every data-touching action "relates to" the data-handling
rule) would force EVERY routine step through principal approval, collapsing the
society to a halt. The obligations are not lost: they remain in the profile and
are committed (SHA-256) into the Charter's `provenance.source_commitments`, so
they are auditable Principal Context — just not a per-delegation gate.
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

    # data_handling / operational / style are standing obligations, not
    # per-delegation gate triggers — see the module docstring for why they are
    # intentionally not projected here. They survive in the profile and in
    # provenance.source_commitments (committed by the seeder).
    return clauses
