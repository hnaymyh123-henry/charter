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

from pydantic import BaseModel, ConfigDict, Field

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
    """

    parent_charter_id: str
    attenuates: dict[str, list[str]] = Field(default_factory=dict)


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
