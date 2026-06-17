"""Charter — agent 经济的雇佣合同（v0 demo）.

Public submodules:
    schema       Pydantic models for Charter / Verdict / RewriteProposal.
    constants    Protocol constants (TYPE_TO_DECISION, aggregate_decision, ...).
    signing      Ed25519 keygen / sign / verify.
    storage      Local JSON / PEM file I/O.
    projection   profile.yaml -> Charter draft (one LLM call).
    server       FastAPI host for Charter JSON.
    mcp_server   fastmcp server exposing the three tools.
    cli          `charter issue` and `charter inspect` commands.
    stepup       Step-up negotiation (B2.5): AdHocGrant + grant verify/apply.
"""

from .stepup import (  # noqa: F401  re-export the B2.5 negotiation surface
    AdHocGrant,
    GrantCheck,
    GrantConstraints,
    GrantLifecycle,
    GrantProvenance,
    GrantVerdict,
    StepUpRequest,
    apply_grant_to_verdict,
    build_step_up_request,
    issue_grant,
    sign_grant,
    validate_grant_targets,
    verify_grant,
    verify_grant_signature,
)

__version__ = "0.1.0"

__all__ = [
    "AdHocGrant",
    "GrantCheck",
    "GrantConstraints",
    "GrantLifecycle",
    "GrantProvenance",
    "GrantVerdict",
    "StepUpRequest",
    "apply_grant_to_verdict",
    "build_step_up_request",
    "issue_grant",
    "sign_grant",
    "validate_grant_targets",
    "verify_grant",
    "verify_grant_signature",
]
