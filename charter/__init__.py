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
"""

__version__ = "0.1.0"
