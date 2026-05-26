"""Pydantic models for Charter v0.

The Charter JSON Schema is the single source of truth that flows between:
    profile.yaml --[projection]--> Charter --[signing]--> Public Charter
                                              \\--[fetch]--> calling agent
                                                                |
                                                                v
                                                  check_compatibility / propose_within_scope

All models below are Pydantic v2 and serialize to JSON with `.model_dump()`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .constants import CHARTER_VERSION

# ---------------------------------------------------------------------------
# Binding & metadata
# ---------------------------------------------------------------------------


class Binding(BaseModel):
    """The `principal × agent` relationship object. Unique key of a Charter."""

    type: Literal["principal_agent"] = "principal_agent"
    principal_id: str
    agent_id: str


class Principal(BaseModel):
    type: Literal["human", "organization", "agent"] = "human"
    id: str
    role_summary: str


class Issuer(BaseModel):
    type: Literal["human", "organization", "agent", "service"] = "human"
    id: str
    relationship_to_principal: str = "self"


class AgentOperator(BaseModel):
    type: Literal["service", "human", "organization"] = "service"
    id: str
    agent_card_url: str | None = None


# ---------------------------------------------------------------------------
# Visibility, summary, clauses
# ---------------------------------------------------------------------------


class Visibility(BaseModel):
    """Per-Charter visibility declarations.

    `private_clauses` widens with each shipped privacy path:

      - `"not_supported_in_v0"` — pre-ADR-011. Every clause is rendered
        in full; no per-span redaction.
      - `"redaction_v1"` — ADR-011 path 1 (SHIPPED). Selected spans
        inside `Clause.text` are replaced with `[REDACTED:<hash-prefix>]`
        placeholders; the issuer publishes only the SHA-256 commitment
        of each redacted value, not the value itself.

    Future paths (delegated grading, ZKPs) will extend the literal
    again. Charters issued before ADR-011 path 1 omit `private_fields`
    on every clause and the field stays at the pre-existing default
    string, so they continue to verify unchanged.
    """

    charter: Literal["public"] = "public"
    raw_principal_context: Literal["private"] = "private"
    private_clauses: Literal["not_supported_in_v0", "redaction_v1"] = "not_supported_in_v0"


class Summary(BaseModel):
    plain_language: str


class PrivateFieldRef(BaseModel):
    """One span inside `Clause.text` that has been replaced with a
    `[REDACTED:<hash-prefix>]` placeholder.

    The span coordinates point INTO the redacted text (i.e. the text
    that appears in the published Charter), so callers can locate the
    placeholder without reconstructing the original. `disclosure_hash`
    is the SHA-256 commitment of `salt || original_value` and is the
    ONLY part of the redacted value that enters the signed canonical
    bytes — the original value lives in a sibling Disclosure record
    that the issuer chooses whether to reveal.
    """

    model_config = ConfigDict(extra="forbid")

    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    disclosure_hash: str  # "sha256:<hex>"


class Clause(BaseModel):
    """A single versioned natural-language clause inside a Charter.

    The `type` field maps deterministically to a local decision via
    `constants.TYPE_TO_DECISION`. The LLM only judges whether the clause is hit
    by the intended task; it does NOT decide allow/needs_approval/incompatible
    on its own.

    When `private_fields` is set (ADR-011 path 1), the clause's `text`
    already contains `[REDACTED:<hash-prefix>]` placeholders in place
    of the sensitive spans. Each `PrivateFieldRef` carries the SHA-256
    commitment of the redacted value; the matching plaintext lives in
    a Disclosure file gated behind `CHARTER_DISCLOSURE_TOKEN`. Older
    Charters omit this field entirely (None) and continue to sign /
    verify without modification.
    """

    id: str
    type: Literal[
        "scope",
        "out_of_scope",
        "approval_required",
        "operational_limit",
        "style",
        "data_handling",
    ]
    text: str
    private_fields: list[PrivateFieldRef] | None = None


# ---------------------------------------------------------------------------
# Decision schema (verdict + matched clauses)
# ---------------------------------------------------------------------------


class MatchedClause(BaseModel):
    """One row in `Verdict.matched_clauses`.

    `source_charter_id` is populated when the verdict was produced by
    `aggregate_verdict_chain` across a Charter Chain; for the single-
    Charter `aggregate_verdict` path it remains `None` (back-compat).
    """

    id: str
    local_decision: Literal["allow", "needs_approval", "incompatible"]
    applied: bool = Field(description="True iff this clause determined the aggregate decision.")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    source_charter_id: str | None = None


class Verdict(BaseModel):
    """Compatibility Check output. Returned by `check_compatibility` MCP tool."""

    decision: Literal["allow", "needs_approval", "incompatible"]
    matched_clauses: list[MatchedClause] = Field(default_factory=list)
    reason: str
    rewrite_available: bool = False


class RewriteProposal(BaseModel):
    """Output of `propose_within_scope` (success case)."""

    rewritten_task: str
    why_in_scope: str
    referenced_clauses: list[str] = Field(default_factory=list)
    remaining_approval_needed: bool = False


class RewriteAttempt(BaseModel):
    """One iteration of the loopback verification loop.

    `verdict` is `None` when the rewrite generation itself failed (LLM
    returned literal `null` or produced unparseable output); otherwise it
    is the verdict that running the per-clause grader + aggregate_verdict
    on the rewritten task produced.
    """

    attempt: int = Field(ge=1)
    temperature: float
    proposal: RewriteProposal | None
    verdict: Verdict | None
    failure_reason: str | None = None


class RewriteFailure(BaseModel):
    """Returned by `propose_within_scope_verified` when no attempt produces
    an in-scope rewrite within `max_attempts`. The full attempt history is
    preserved so callers can surface what was tried."""

    attempts: list[RewriteAttempt]
    reason: str


class AP2VerifyResult(BaseModel):
    """Result of running an AP2 Mandate through the Charter compatibility
    check (see `charter.adapters.ap2.verify`).

    Charter and AP2 verify orthogonal axes — Charter answers "is the agent
    allowed to do this kind of work for this principal at all?", AP2
    answers "is this specific transaction authorized?". A safe delegation
    requires both to pass, which is what `final_decision` collapses.

    Attributes:
        mandate_ok:
            True iff the AP2 mandate itself passes the host's mandate
            integrity check (signature + lifetime + scope).
        charter_verdict:
            Verdict from running `aggregate_verdict` against the Charter
            referenced by `extensions.charter_url`. `None` when the
            Charter could not be fetched / verified at all (in which case
            `final_decision` is `incompatible`).
        final_decision:
            Collapsed verdict combining both layers. See the rule table
            in `charter.adapters.ap2.verify`.
        reason:
            Human-readable explanation; safe to surface to operators or
            log lines.
    """

    mandate_ok: bool
    charter_verdict: Verdict | None
    final_decision: Literal["allow", "needs_approval", "incompatible"]
    reason: str


# ---------------------------------------------------------------------------
# Lifecycle & provenance
# ---------------------------------------------------------------------------


class Lifecycle(BaseModel):
    issued_at: datetime
    valid_until: datetime
    status: Literal["active", "expired", "revoked", "superseded"] = "active"
    revoked_at: datetime | None = None
    replaces: str | None = None
    replaced_by: str | None = None


class SourceCommitment(BaseModel):
    """A non-revealing summary of the source material used during projection.

    Only the SHA-256 hash of the raw content is published — never the content
    itself. This preserves the Principal Context / Public Charter boundary.
    """

    type: str  # e.g. "profile_yaml"
    description: str
    content_hash: str  # "sha256:<hex>"


class Provenance(BaseModel):
    """Public provenance of a Charter.

    `issuer_public_key` is embedded inline (Self-Attesting Charter, §P2-9).
    Calling agents verify `issuer_signature` against this public key directly,
    using HTTPS to charter_url as the trust root.

    v0.8+: when an issuer publishes a JWKS at
    `{issuer_origin}/.well-known/jwks.json`, `issuer_kid` lets the caller
    route to the right key in the JWKS without scanning. Charters issued
    before v0.8 leave it `None`; verification falls back to the inline
    `issuer_public_key`.

    `transparency_log_id` is the `seq` of this Charter's entry in the
    issuer's transparency log. Populated by `sign_charter` AFTER the
    signature is computed and the log entry is appended. NOT covered by
    the signature (it has to be excluded from the canonical bytes — the
    log entry can only be written after the signature is final). Lets a
    calling agent jump straight to `{issuer_origin}/transparency/proof/...`
    without scanning the log. `None` for Charters issued before v0.8 or
    when the log was not reachable at sign time.
    """

    issuer_public_key: str  # "ed25519:<base64>"
    issuer_signature: str = ""  # "ed25519:<base64>" — set during signing
    issuer_kid: str | None = None  # JWKS key id; populated by sign_charter
    transparency_log_id: int | None = None  # log seq; populated by sign_charter
    source_commitments: list[SourceCommitment] = Field(default_factory=list)
    generated_at: datetime


# ---------------------------------------------------------------------------
# Charter Chain (parent reference + attenuation proof)
# ---------------------------------------------------------------------------


class SemanticCheckResult(BaseModel):
    """One cached LLM verdict from `verify_chain_semantic`.

    Cached results are keyed by `f"{parent_charter_id}@{parent.lifecycle.issued_at}"`
    inside `AttenuationProof.semantic_check_cache`, so any re-sign of the
    parent (which bumps `issued_at`) invalidates stale verdicts automatically.

    Attributes:
        matches_subset:
            True iff the LLM judged child clauses to semantically cover
            every parent restriction.
        reason:
            One short sentence summarizing the grader's reasoning. Stored
            for audit; not consumed by `verify_chain_semantic` itself.
        graded_at:
            Wall-clock timestamp when the verdict was produced. Audit
            field; verifiers do not gate on it.
    """

    matches_subset: bool
    reason: str
    graded_at: datetime


class AttenuationProof(BaseModel):
    """A child Charter's declarative claim that it is a stricter subset
    of its parent.

    The protocol's `verify_chain` checks the claim against the actual
    clauses — the proof is metadata for auditors, not a substitute for
    verification.

    Attributes:
        parent_charter_id:
            The `charter_id` of the parent Charter this one attenuates.
            Must match the parent's `charter_id` resolved via
            `parent_charter_url`; mismatch is a chain-validation failure.
        attenuates:
            For each child clause id, the parent clause ids it tightens
            or inherits. Optional — used for audit traceability. Missing
            entries do not invalidate the chain; `verify_chain` checks
            the actual clause content.
        semantic_check_cache:
            Memoization of `verify_chain_semantic` verdicts keyed by
            `f"{parent_charter_id}@{parent.lifecycle.issued_at.isoformat()}"`.
            The key embeds `issued_at` so re-signing a parent invalidates
            all of its cached verdicts. Empty for Charters that have never
            been semantically verified. Determinism guarantee: once a
            verdict is cached, subsequent `verify_chain_semantic` calls
            return the same bool without invoking the LLM.
    """

    parent_charter_id: str
    attenuates: dict[str, list[str]] = Field(default_factory=dict)
    semantic_check_cache: dict[str, SemanticCheckResult] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Charter (top-level)
# ---------------------------------------------------------------------------


class DecisionSchemaDoc(BaseModel):
    """Inline schema documentation embedded in every Charter so callers know
    what `Verdict` shape to expect. Not used at runtime — informational only.
    """

    decision: str = "allow | needs_approval | incompatible"
    matched_clauses: str = "[{id, local_decision, applied, confidence in [0,1], reason}]"
    reason: str = "string -- short summary referencing applied clauses"
    rewrite_available: str = "bool -- whether propose_within_scope might help"


class Charter(BaseModel):
    """Top-level Charter object. Serialize via `.model_dump_json(indent=2)`."""

    model_config = ConfigDict(extra="forbid")

    version: str = CHARTER_VERSION
    charter_id: str

    binding: Binding
    principal: Principal
    issuer: Issuer
    agent_operator: AgentOperator
    principal_chain: list[str] = Field(default_factory=list)

    visibility: Visibility = Field(default_factory=Visibility)
    summary: Summary
    clauses: list[Clause]
    decision_schema: DecisionSchemaDoc = Field(default_factory=DecisionSchemaDoc)
    lifecycle: Lifecycle
    provenance: Provenance

    # Charter Chain fields. Both `None` for root Charters; both populated
    # for child Charters in an attenuation chain. `verify_chain` checks
    # the subset relation against the parent fetched via this URL.
    parent_charter_url: str | None = None
    attenuation_proof: AttenuationProof | None = None


# ---------------------------------------------------------------------------
# AdHocGrant (B2.5 — step-up protocol, ADR-013)
# ---------------------------------------------------------------------------


class AdHocGrant(BaseModel):
    """A short-TTL, task-bound, signed ad-hoc extension of issuer authority.

    The dual of `propose_within_scope`. Where the rewrite path adjusts the
    *task* to fit the Charter, an `AdHocGrant` temporarily authorises the
    *task* outside the Charter's normal scope. The grant is **a sibling of
    Charter, not a child** — no Charter field is mutated. A grant is
    signed with the same Ed25519 issuer key as the Charter, has its own
    canonical-bytes rule (sorted keys, `issuer_signature` excluded), and
    has no `revoked` lifecycle state: short TTL is the only safety
    primitive (ADR-013).

    Why no `revoked` state: the threat model is "issuer wants to widen
    authority for exactly this one task for exactly this many seconds".
    Adding a `revoked` flag would require every consumer to fetch the
    grant on every use; the design choice is instead to keep TTL short
    (default 300s, max 3600s) and physically delete the grant file when
    early teardown is needed. Charters keep their revoke story; grants
    don't get one.

    Why a Charter revoke does NOT invalidate live grants: a grant is its
    own signed token. Once the issuer issues it, it stands on its own
    feet until expiry. This avoids a stale-grants-after-revoke cache
    invalidation problem at the cost of one design choice that ADR-013
    spells out: revoking a Charter is the right tool for "this agent
    can never act for me again"; grants are for "this one task in the
    next 5 minutes". They are orthogonal.
    """

    model_config = ConfigDict(extra="forbid")

    grant_id: str  # UUID v4 (hex)
    charter_url: str  # the Charter this grant attaches to (NOT mutated)
    task: str  # natural-language task description submitted by caller
    justification: str  # caller-supplied reason, shown to principal at approval time
    granted_at: datetime
    expires_at: datetime  # short TTL, max 3600s by default
    issuer_signature: str = ""  # "ed25519:<base64>"; set during signing
    issuer_kid: str  # same kid as the underlying Charter's provenance.issuer_kid
    approval_metadata: dict[str, str] | None = None  # optional context from approval

    @model_validator(mode="after")
    def _validate_ttl_bounds(self) -> AdHocGrant:
        """Enforce TTL invariants:
          - expires_at must be strictly after granted_at.
          - expires_at - granted_at must be >= 60s (no instant-burn grants).
          - expires_at - granted_at must be <= 3600s (the protocol cap).

        These bounds protect both directions: a too-short TTL is racy
        and undermines the step-up flow (caller cannot use it before
        it expires); a too-long TTL erodes the "temporary widening"
        invariant that justifies grants being short-cut tokens rather
        than full charter modifications (ADR-013).
        """
        delta = (self.expires_at - self.granted_at).total_seconds()
        if delta < 60:
            raise ValueError(f"AdHocGrant TTL must be >= 60s (got {delta:.0f}s)")
        if delta > 3600:
            raise ValueError(f"AdHocGrant TTL must be <= 3600s (got {delta:.0f}s)")
        return self


class AdHocGrantRequest(BaseModel):
    """Input to `POST /step-up`.

    The caller asks the issuer to widen authority for one specific
    task. `max_ttl_seconds` defaults to 300 (5 minutes); the server
    caps it at `CHARTER_STEPUP_MAX_TTL` (default 3600s, set by env).
    Anything above the cap returns 400.
    """

    model_config = ConfigDict(extra="forbid")

    charter_url: str
    task: str
    justification: str
    max_ttl_seconds: int = Field(default=300, ge=60)


class AdHocGrantResponse(BaseModel):
    """Output of `POST /step-up`.

    Three outcomes:
        approved:  The issuer signed a grant. `grant` is populated.
        pending:   Asynchronous approval is in-flight (callback mode
                   with non-immediate response). `grant` is None. The
                   reference implementation does NOT use this state
                   today — the callback hook is synchronous in v0.9.
        denied:    The issuer refused (default `auto-deny` mode, or
                   the callback target returned denial). `grant` is
                   None and `denial_reason` carries a human-readable
                   string.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["approved", "pending", "denied"]
    grant: AdHocGrant | None = None
    denial_reason: str | None = None


# ---------------------------------------------------------------------------
# Profile (input to `charter issue`)
# ---------------------------------------------------------------------------


class ProfilePrincipal(BaseModel):
    id: str
    role: str


class ProfileAgent(BaseModel):
    id: str
    card_url: str | None = None


class ProfileDataHandling(BaseModel):
    what: str = ""
    rules: str = ""


class ProfileOperational(BaseModel):
    hours: str = "anytime"
    budget_per_task_usd: float | None = None
    budget_monthly_usd: float | None = None


class ProfileLifecycle(BaseModel):
    valid_days: int = 30


class Profile(BaseModel):
    """The 10-field profile.yaml schema.

    Consumed by `charter issue`. Treated as Principal Context — only its
    SHA-256 commitment is written to provenance.source_commitments.
    """

    model_config = ConfigDict(extra="forbid")

    principal: ProfilePrincipal
    agent: ProfileAgent
    scope: list[str]
    out_of_scope: list[str] = Field(default_factory=list)
    approval_required: list[str] = Field(default_factory=list)
    data_handling: ProfileDataHandling | None = None
    operational: ProfileOperational | None = None
    style: str | None = None
    lifecycle: ProfileLifecycle = Field(default_factory=ProfileLifecycle)
