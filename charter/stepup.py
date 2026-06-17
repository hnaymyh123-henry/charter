"""Step-up negotiation protocol (ROADMAP B2.5).

This module adds a *negotiation* layer ON TOP OF the frozen Charter protocol
core. When a routing gate produces `decision == "needs_approval"`, a calling
agent can escalate to the principal; if the principal approves, they issue a
signed, narrowly-scoped, single-use ``AdHocGrant`` that lets exactly one task
proceed.

The single most important property of this module — the RED LINE — is:

    An AdHocGrant can ONLY ever downgrade ``needs_approval`` to ``allow`` for
    the explicit clauses the principal named. It is STRUCTURALLY INCAPABLE of
    relaxing ``incompatible`` (out_of_scope), a revoked charter, or a
    signature-verification failure.

Three layers enforce that red line, so a bug in any one is caught by another:

  1. **Construction-time validation** (``validate_grant_targets``): every clause
     id listed in ``relaxes_clause_ids`` must map — via the charter's own
     ``TYPE_TO_DECISION`` — to a clause whose local decision is
     ``needs_approval``. A clause of type ``out_of_scope`` (→ incompatible) or
     ``scope`` / ``style`` (→ allow) is REJECTED at grant-creation time. You
     cannot even build a grant that claims to waive an out_of_scope clause.

  2. **Escalation-time refusal** (``request_step_up`` / ``build_step_up_request``):
     the step-up request itself is refused unless the failed verdict's decision
     is exactly ``needs_approval``. ``incompatible`` is terminal; there is no
     negotiation path out of it.

  3. **Apply-time short-circuit** (``apply_grant``): the base verdict is
     recomputed from scratch via the frozen ``aggregate_verdict``. If that base
     verdict contains ANY ``incompatible`` clause, ``effective_decision`` stays
     ``incompatible`` REGARDLESS of the grant, before any downgrade logic runs.
     Only matched clauses whose ``local_decision == "needs_approval"`` AND whose
     id is in ``relaxes_clause_ids`` AND for which the grant fully verifies are
     downgraded.

A grant is principal-authored authority, exactly like a Charter, so it reuses
the SAME Ed25519 signing primitives (``charter.signing``) and the
``ed25519:<b64>`` string convention. ``verify_grant`` mirrors ``verify_charter``
(signature + lifecycle/expiry/consumed checks).

Grants are single-use: a successful ``apply_grant`` marks the persisted grant
``consumed`` so it cannot authorize a second task.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field

from .constants import TYPE_TO_DECISION, Decision
from .schema import Verdict
from .signing import public_key_from_string

# ---------------------------------------------------------------------------
# Grant component models
# ---------------------------------------------------------------------------


class GrantConstraints(BaseModel):
    """The narrowing the principal attaches to an AdHocGrant.

    Every field tightens what the grant authorizes; none can widen it.
    Defaults are intentionally restrictive (single-use, short TTL).
    """

    model_config = ConfigDict(extra="forbid")

    one_shot: bool = True
    allowed_recipients: list[str] = Field(default_factory=list)
    allowed_scope: str | None = None
    max_budget_usd: float | None = None
    ttl_seconds: int = 600


class GrantLifecycle(BaseModel):
    """Issuance / expiry window + status. Mirrors ``schema.Lifecycle`` but for
    a grant rather than a Charter. ``status`` transitions are one-way:
    ``active`` -> ``consumed`` (on first successful apply) or
    ``active`` -> ``expired`` / ``revoked``.
    """

    model_config = ConfigDict(extra="forbid")

    issued_at: datetime
    expires_at: datetime
    status: Literal["active", "consumed", "expired", "revoked"] = "active"


class GrantProvenance(BaseModel):
    """Public provenance of an AdHocGrant.

    Same self-attesting model as ``Charter.provenance``: the principal's public
    key is embedded inline, and ``issuer_signature`` is an Ed25519 signature
    over the grant's canonical bytes (with the signature field cleared).
    """

    model_config = ConfigDict(extra="forbid")

    issuer_public_key: str  # "ed25519:<base64>"
    issuer_signature: str = ""  # "ed25519:<base64>" — set during signing
    generated_at: datetime


# ---------------------------------------------------------------------------
# AdHocGrant
# ---------------------------------------------------------------------------


class AdHocGrant(BaseModel):
    """A principal-signed, single-use, scoped temporary authorization.

    RED LINE: only relaxes ``needs_approval`` (from approval_required /
    operational_limit / data_handling clauses). It is structurally incapable of
    relaxing ``incompatible`` / revoked / signature-failure — see the module
    docstring and ``apply_grant``.

    The grant binds to a *specific* charter revision AND a *specific* task:

      - ``charter_url`` + ``charter_id`` pin the exact worker charter the grant
        is scoped to (both must match at apply time).
      - ``task_id`` pins the single delegate_task this grant authorizes.

    Re-presenting a grant against a different charter, a different charter
    revision, or a different task is rejected by ``verify_grant``.
    """

    model_config = ConfigDict(extra="forbid")

    grant_id: str  # "grant:<charter_id>:<task_id>:<short-uuid>"
    charter_url: str  # exact-match required at apply time
    charter_id: str  # binds grant to a specific charter revision
    task_id: str  # the delegate_task task_id this grant authorizes — single-use
    relaxes_clause_ids: list[str]  # clause ids whose needs_approval is waived
    constraints: GrantConstraints
    lifecycle: GrantLifecycle
    provenance: GrantProvenance
    reason: str  # human audit string

    # Optional: the principal/agent identities for audit. Not load-bearing for
    # the red line, but useful in the transparency log.
    issued_by: str | None = None  # principal_id that authored the grant


# ---------------------------------------------------------------------------
# StepUpRequest (worker -> principal escalation payload)
# ---------------------------------------------------------------------------


class StepUpRequest(BaseModel):
    """Worker -> principal escalation payload.

    Emitted ONLY when ``verdict.decision == "needs_approval"``. The hard
    invariant lives in ``build_step_up_request`` / the ``request_step_up`` MCP
    tool: any other decision is refused. This is the structural red line at the
    escalation boundary.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    charter_url: str
    charter_id: str
    intended_task: str
    verdict: Verdict
    requested_clause_ids: list[str]  # the applied needs_approval clause ids
    justification: str


# ---------------------------------------------------------------------------
# GrantVerdict (output of apply_grant — the re-gate after a grant is presented)
# ---------------------------------------------------------------------------


class GrantVerdict(BaseModel):
    """Output of ``apply_grant``: the re-gate after a grant is presented.

    ``granted`` is True only when every ``needs_approval`` clause was covered by
    a valid grant AND no ``incompatible`` clause exists. ``effective_decision``
    is the post-grant decision and never invents a 4th value — it is always one
    of the frozen ``allow | needs_approval | incompatible``.
    """

    model_config = ConfigDict(extra="forbid")

    granted: bool
    effective_decision: Decision
    grant_id: str | None
    verdict: Verdict  # the original (base) verdict, unmodified
    reason: str


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def make_grant_id(charter_id: str, task_id: str) -> str:
    """Construct a grant id: ``grant:<charter_id>:<task_id>:<short-uuid>``."""
    return f"grant:{charter_id}:{task_id}:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Construction-time validation — RED LINE layer 1
# ---------------------------------------------------------------------------


def _clause_types(charter: dict[str, Any]) -> dict[str, str]:
    """Map clause id -> clause type from a Charter dict."""
    return {c["id"]: c["type"] for c in (charter.get("clauses") or [])}


def validate_grant_targets(charter: dict[str, Any], relaxes_clause_ids: list[str]) -> list[str]:
    """Return the subset of ``relaxes_clause_ids`` that a grant is ALLOWED to
    waive, raising ``ValueError`` if ANY requested clause is not waivable.

    A clause is waivable iff it exists in the charter AND its type maps (via the
    frozen ``TYPE_TO_DECISION``) to ``needs_approval``. That is exactly the set
    {approval_required, operational_limit, data_handling}.

    REJECTED (raise ValueError):
      - clause id not present in the charter at all,
      - ``out_of_scope`` clause (→ incompatible — the red line; never grantable),
      - ``scope`` / ``style`` clause (→ allow — nothing to grant).

    This is RED-LINE layer 1: you cannot even CONSTRUCT a grant that claims to
    waive an out_of_scope clause, so the dangerous grant never exists on disk.
    """
    if not relaxes_clause_ids:
        raise ValueError("a grant must waive at least one needs_approval clause")

    types = _clause_types(charter)
    waivable: list[str] = []
    for cid in relaxes_clause_ids:
        ctype = types.get(cid)
        if ctype is None:
            raise ValueError(f"clause {cid!r} is not present in charter {charter.get('charter_id')!r}")
        local = TYPE_TO_DECISION.get(ctype)
        if local != "needs_approval":
            raise ValueError(
                f"clause {cid!r} (type={ctype!r}) maps to local_decision={local!r}; "
                f"a grant may only waive needs_approval clauses "
                f"(approval_required / operational_limit / data_handling). "
                f"out_of_scope (incompatible) clauses are NEVER grantable — red line."
            )
        waivable.append(cid)
    return waivable


# ---------------------------------------------------------------------------
# Canonical bytes / sign / verify — reuse the Charter ed25519 path
# ---------------------------------------------------------------------------


def _canonical_grant_bytes(grant: AdHocGrant) -> bytes:
    """Serialize a grant for signing with ``issuer_signature`` cleared AND
    ``lifecycle.status`` normalized to ``active``.

    Same convention as ``charter.signing._canonical_bytes``: sorted keys,
    compact separators, signature field zeroed to break the self-reference.

    ``lifecycle.status`` is EXCLUDED from the signed bytes (normalized to
    ``active``) for the same reason ``transparency_log_id`` is excluded from a
    Charter's canonical bytes: it is RUNTIME state set after issuance, not
    principal-authored content. The principal signs "an active grant for task X
    waiving clauses Y"; the server later flips status to ``consumed`` (single-use
    enforcement) WITHOUT holding the private key. If status were in the signed
    bytes, consuming the grant would invalidate its own signature and the
    single-use check could never be distinguished from a forgery. Excluding it
    keeps the two failure modes — "signature forged" vs "already consumed" —
    cleanly separable, which the red-line tests rely on.
    """
    payload = grant.model_dump(mode="json")
    payload["provenance"]["issuer_signature"] = ""
    payload["lifecycle"]["status"] = "active"
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_grant(grant: AdHocGrant, private_key: Ed25519PrivateKey) -> AdHocGrant:
    """Sign a grant in place and return it.

    The caller must set ``provenance.issuer_public_key`` to match this private
    key before calling (same contract as ``sign_charter``). The principal's key
    is the SAME issuer key used to sign that principal's Charters, so the trust
    model stays uniform — a grant is just another signed object from the same
    authority.
    """
    payload = _canonical_grant_bytes(grant)
    signature = private_key.sign(payload)
    grant.provenance.issuer_signature = f"ed25519:{base64.b64encode(signature).decode('ascii')}"
    return grant


def verify_grant_signature(grant: AdHocGrant) -> bool:
    """Verify a grant's Ed25519 signature against its embedded public key.

    Mirrors ``verify_charter``: returns True iff the signature is valid. Does
    NOT check lifecycle/expiry/consumed — those are policy checks done by
    ``verify_grant``.
    """
    sig_str = grant.provenance.issuer_signature
    if not sig_str.startswith("ed25519:"):
        return False
    try:
        signature = base64.b64decode(sig_str.removeprefix("ed25519:"))
        public_key = public_key_from_string(grant.provenance.issuer_public_key)
        payload = _canonical_grant_bytes(grant)
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Full grant verification (signature + binding + lifecycle + constraints)
# ---------------------------------------------------------------------------


class GrantCheck(BaseModel):
    """Result of ``verify_grant`` — a valid/invalid flag plus the reason.

    The ``reason`` is safe to put in the transparency log; ``ok=False`` is the
    fail-closed default for every failure mode (bad sig, expired, consumed,
    wrong charter/task, unsatisfied constraints).
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str


def verify_grant(
    grant: AdHocGrant,
    *,
    charter: dict[str, Any],
    charter_url: str,
    task_id: str,
    now: datetime | None = None,
    recipients: list[str] | None = None,
    budget_usd: float | None = None,
) -> GrantCheck:
    """Validate a grant against the context it is being presented in.

    Fail-closed: returns ``GrantCheck(ok=False, ...)`` on the FIRST failing
    check. Checks, in order:

      1. Signature verifies against the embedded principal public key.
      2. ``grant.charter_id`` matches the charter being gated (revision pin).
      3. ``grant.charter_url`` matches ``charter_url`` (exact-match binding).
      4. ``grant.task_id`` matches ``task_id`` (single task this authorizes).
      5. Lifecycle status is ``active`` (not consumed / expired / revoked).
      6. Not past ``expires_at`` (using ``now`` or wall clock).
      7. Constraints satisfied: requested recipients ⊆ allowed_recipients
         (when allowed_recipients is non-empty), and ``budget_usd`` ≤
         ``max_budget_usd`` (when a budget cap is set).

    NOTE: this does NOT decide allow/needs_approval — that is ``apply_grant``'s
    job. ``verify_grant`` only answers "is this grant itself trustworthy and
    in-context?".
    """
    ts = now or datetime.now(UTC)

    if not verify_grant_signature(grant):
        return GrantCheck(ok=False, reason="grant signature did not verify")

    charter_id = charter.get("charter_id")
    if grant.charter_id != charter_id:
        return GrantCheck(
            ok=False,
            reason=f"grant.charter_id {grant.charter_id!r} != charter {charter_id!r}",
        )

    if grant.charter_url != charter_url:
        return GrantCheck(
            ok=False,
            reason=f"grant.charter_url {grant.charter_url!r} != {charter_url!r}",
        )

    if grant.task_id != task_id:
        return GrantCheck(
            ok=False,
            reason=f"grant.task_id {grant.task_id!r} != task {task_id!r} (single-use binding)",
        )

    status = grant.lifecycle.status
    if status != "active":
        return GrantCheck(ok=False, reason=f"grant status={status!r}; only 'active' grants apply")

    expires = grant.lifecycle.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if ts >= expires:
        return GrantCheck(ok=False, reason=f"grant expired at {expires.isoformat()}")

    # Constraint: recipient allowlist.
    allowed = grant.constraints.allowed_recipients
    if allowed and recipients:
        extra = [r for r in recipients if r not in allowed]
        if extra:
            return GrantCheck(
                ok=False,
                reason=f"recipients {extra} not in grant allowlist {allowed}",
            )

    # Constraint: budget cap.
    cap = grant.constraints.max_budget_usd
    if cap is not None and budget_usd is not None and budget_usd > cap:
        return GrantCheck(
            ok=False,
            reason=f"requested budget {budget_usd} exceeds grant cap {cap}",
        )

    return GrantCheck(ok=True, reason="grant valid")


# ---------------------------------------------------------------------------
# Grant minting helper (principal side)
# ---------------------------------------------------------------------------


def issue_grant(
    *,
    charter: dict[str, Any],
    charter_url: str,
    task_id: str,
    relaxes_clause_ids: list[str],
    private_key: Ed25519PrivateKey,
    issuer_public_key: str,
    reason: str,
    constraints: GrantConstraints | None = None,
    issued_by: str | None = None,
    now: datetime | None = None,
) -> AdHocGrant:
    """Build, validate, and sign a single AdHocGrant (principal side).

    ``validate_grant_targets`` runs FIRST (red-line layer 1): if any requested
    clause is not a ``needs_approval`` clause, this raises ``ValueError`` and no
    grant object is ever produced.

    The grant's ``expires_at`` is derived from ``constraints.ttl_seconds`` so the
    lifecycle and constraints stay consistent.
    """
    # RED LINE layer 1: refuse to mint a grant targeting non-needs_approval clauses.
    waivable = validate_grant_targets(charter, relaxes_clause_ids)

    cons = constraints or GrantConstraints()
    ts = now or datetime.now(UTC).replace(microsecond=0)
    charter_id = str(charter.get("charter_id"))

    grant = AdHocGrant(
        grant_id=make_grant_id(charter_id, task_id),
        charter_url=charter_url,
        charter_id=charter_id,
        task_id=task_id,
        relaxes_clause_ids=waivable,
        constraints=cons,
        lifecycle=GrantLifecycle(
            issued_at=ts,
            expires_at=ts + timedelta(seconds=cons.ttl_seconds),
            status="active",
        ),
        provenance=GrantProvenance(
            issuer_public_key=issuer_public_key,
            issuer_signature="",
            generated_at=ts,
        ),
        reason=reason,
        issued_by=issued_by,
    )
    return sign_grant(grant, private_key)


# ---------------------------------------------------------------------------
# StepUpRequest builder — RED LINE layer 2 (escalation boundary)
# ---------------------------------------------------------------------------


def build_step_up_request(
    *,
    task_id: str,
    charter_url: str,
    charter_id: str,
    intended_task: str,
    failed_verdict: Verdict,
    justification: str,
) -> StepUpRequest | None:
    """Build a StepUpRequest, or return ``None`` if escalation is not permitted.

    RED-LINE layer 2: returns ``None`` unless ``failed_verdict.decision`` is
    exactly ``needs_approval``. ``incompatible`` (and ``allow``) are not
    negotiable — there is no path from an incompatible verdict to a step-up
    request, so the negotiation protocol can never be used to bypass a hard
    limit.

    The requested clause ids are exactly the APPLIED clauses whose
    ``local_decision == "needs_approval"`` — i.e. the clauses that actually
    forced the needs_approval outcome, not every matched clause.
    """
    if failed_verdict.decision != "needs_approval":
        return None

    requested = [
        m.id
        for m in failed_verdict.matched_clauses
        if m.applied and m.local_decision == "needs_approval"
    ]

    return StepUpRequest(
        task_id=task_id,
        charter_url=charter_url,
        charter_id=charter_id,
        intended_task=intended_task,
        verdict=failed_verdict,
        requested_clause_ids=requested,
        justification=justification,
    )


# ---------------------------------------------------------------------------
# apply_grant — RED LINE layer 3 (the post-grant re-gate)
# ---------------------------------------------------------------------------


def apply_grant_to_verdict(
    base_verdict: Verdict,
    grant: AdHocGrant | None,
    grant_check: GrantCheck | None,
) -> GrantVerdict:
    """Pure decision core of ``apply_grant``: combine a (recomputed) base
    verdict with a grant and produce a ``GrantVerdict``.

    This function does NOT recompute the base verdict, fetch charters, or touch
    disk — it is the deterministic, side-effect-free heart of the red line, so
    it can be unit-tested in isolation. The MCP tool ``apply_grant`` wraps it
    with charter fetch + ``aggregate_verdict`` recomputation + persistence.

    Rules (in strict order):

      1. **incompatible short-circuit (RED LINE).** If ``base_verdict.decision``
         is ``incompatible``, return it UNCHANGED, ``granted=False``, no matter
         what the grant says. This runs before any downgrade logic.

      2. If there is no grant, or the grant failed ``verify_grant``
         (``grant_check.ok is False``), return the base verdict unchanged.

      3. Otherwise, for each matched clause whose ``local_decision`` is
         ``needs_approval`` AND whose id is in ``grant.relaxes_clause_ids``,
         treat that clause as covered. If EVERY needs_approval clause is covered
         → ``effective_decision = allow``, ``granted=True``. If any
         needs_approval clause is left uncovered → ``effective_decision`` stays
         ``needs_approval``, ``granted=False``.
    """
    # RED LINE layer 3: incompatible is terminal, full stop.
    if base_verdict.decision == "incompatible":
        return GrantVerdict(
            granted=False,
            effective_decision="incompatible",
            grant_id=grant.grant_id if grant else None,
            verdict=base_verdict,
            reason=(
                "base decision is incompatible (out_of_scope); a grant CANNOT "
                "relax a hard limit — request refused at the red line."
            ),
        )

    # No grant or invalid grant: base verdict stands.
    if grant is None or grant_check is None or not grant_check.ok:
        reason = (
            "no grant presented" if grant is None
            else f"grant rejected: {grant_check.reason if grant_check else 'unknown'}"
        )
        return GrantVerdict(
            granted=False,
            effective_decision=base_verdict.decision,
            grant_id=grant.grant_id if grant else None,
            verdict=base_verdict,
            reason=reason,
        )

    # If the base verdict is already allow, there is nothing to downgrade.
    if base_verdict.decision == "allow":
        return GrantVerdict(
            granted=False,
            effective_decision="allow",
            grant_id=grant.grant_id,
            verdict=base_verdict,
            reason="base decision already allow; grant not needed.",
        )

    # base_verdict.decision == "needs_approval": try to cover every NA clause.
    waived = set(grant.relaxes_clause_ids)
    needs_approval_clauses = [
        m for m in base_verdict.matched_clauses if m.local_decision == "needs_approval"
    ]
    uncovered = [m.id for m in needs_approval_clauses if m.id not in waived]

    if uncovered:
        return GrantVerdict(
            granted=False,
            effective_decision="needs_approval",
            grant_id=grant.grant_id,
            verdict=base_verdict,
            reason=(
                f"grant does not cover all needs_approval clauses; "
                f"still pending: {uncovered}"
            ),
        )

    covered = [m.id for m in needs_approval_clauses]
    return GrantVerdict(
        granted=True,
        effective_decision="allow",
        grant_id=grant.grant_id,
        verdict=base_verdict,
        reason=(
            f"grant {grant.grant_id} waives all needs_approval clauses {covered}; "
            f"effective decision downgraded to allow (single-use)."
        ),
    )
