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
from typing import Literal, Optional

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
    agent_card_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Visibility, summary, clauses
# ---------------------------------------------------------------------------

class Visibility(BaseModel):
    charter: Literal["public"] = "public"
    raw_principal_context: Literal["private"] = "private"
    private_clauses: Literal["not_supported_in_v0"] = "not_supported_in_v0"


class Summary(BaseModel):
    plain_language: str


class Clause(BaseModel):
    """A single versioned natural-language clause inside a Charter.

    The `type` field maps deterministically to a local decision via
    `constants.TYPE_TO_DECISION`. The LLM only judges whether the clause is hit
    by the intended task; it does NOT decide allow/needs_approval/incompatible
    on its own.
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


# ---------------------------------------------------------------------------
# Decision schema (verdict + matched clauses)
# ---------------------------------------------------------------------------

class MatchedClause(BaseModel):
    """One row in `Verdict.matched_clauses` — structured per §P2-11 ⑬."""
    id: str
    local_decision: Literal["allow", "needs_approval", "incompatible"]
    applied: bool = Field(
        description="True iff this clause determined the aggregate decision."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class Verdict(BaseModel):
    """Compatibility Check output. Returned by `check_compatibility` MCP tool."""
    decision: Literal["allow", "needs_approval", "incompatible"]
    matched_clauses: list[MatchedClause] = Field(default_factory=list)
    reason: str
    rewrite_available: bool = False


class RewriteProposal(BaseModel):
    """Output of `propose_within_scope` MCP tool (success case).

    v0 implementation note: this is a single-shot LLM output, no loopback
    verification, no retry. See §P2-10 (deferred to v0+).
    """
    rewritten_task: str
    why_in_scope: str
    referenced_clauses: list[str] = Field(default_factory=list)
    remaining_approval_needed: bool = False


# ---------------------------------------------------------------------------
# Lifecycle & provenance
# ---------------------------------------------------------------------------

class Lifecycle(BaseModel):
    issued_at: datetime
    valid_until: datetime
    status: Literal["active", "expired", "revoked", "superseded"] = "active"
    revoked_at: Optional[datetime] = None
    replaces: Optional[str] = None
    replaced_by: Optional[str] = None


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
    """
    issuer_public_key: str          # "ed25519:<base64>"
    issuer_signature: str = ""      # "ed25519:<base64>" — set during signing
    source_commitments: list[SourceCommitment] = Field(default_factory=list)
    generated_at: datetime


# ---------------------------------------------------------------------------
# Charter (top-level)
# ---------------------------------------------------------------------------

class DecisionSchemaDoc(BaseModel):
    """Inline schema documentation embedded in every Charter so callers know
    what `Verdict` shape to expect. Not used at runtime — informational only.
    """
    decision: str = "allow | needs_approval | incompatible"
    matched_clauses: str = (
        "[{id, local_decision, applied, confidence in [0,1], reason}]"
    )
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


# ---------------------------------------------------------------------------
# Profile (input to `charter issue`)
# ---------------------------------------------------------------------------

class ProfilePrincipal(BaseModel):
    id: str
    role: str


class ProfileAgent(BaseModel):
    id: str
    card_url: Optional[str] = None


class ProfileDataHandling(BaseModel):
    what: str = ""
    rules: str = ""


class ProfileOperational(BaseModel):
    hours: str = "anytime"
    budget_per_task_usd: Optional[float] = None
    budget_monthly_usd: Optional[float] = None


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
    data_handling: Optional[ProfileDataHandling] = None
    operational: Optional[ProfileOperational] = None
    style: Optional[str] = None
    lifecycle: ProfileLifecycle = Field(default_factory=ProfileLifecycle)
