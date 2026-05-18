# Charter Context

> **What this file is:** the canonical glossary. Every term used in
> [`PRODUCT.md`](PRODUCT.md), [`ROADMAP.md`](ROADMAP.md), and
> [`AGENTS.md`](AGENTS.md) is defined here. For the full product
> description — what Charter is, how it works, what's shipped, and
> what's next — start with [`PRODUCT.md`](PRODUCT.md).

## Purpose

Charter is a personal authority layer for the agent economy. It describes the relationship between a principal and an agent: who the agent is acting for, what it is allowed or willing to do in that principal context, and how another agent should check those constraints before delegation.

## Core Terms

### Agent

An executable actor that can be delegated work by another human or agent. In this project, the same underlying agent can behave differently under different Charters because its technical capability is not the same as its principal-scoped authority.

### Principal

The person, organization, or upstream agent on whose behalf an agent acts. The principal supplies the context that determines the agent's allowed scope, refusals, caveats, operating limits, and escalation behavior.

### Principal Context

The private source material used to draft a Charter, such as profile answers, memory, profile notes, policy preferences, or conversation history. Principal Context is not part of the public Charter.

### Profile

A YAML file containing 10 top-level fields the principal fills in to declare their identity, the target agent, scope, out-of-scope topics, approval-required actions, data-handling rules, operational limits, style preferences, and Charter validity period. The Profile replaces the earlier interactive "wizard" concept; it is consumed in one shot by the `charter issue` CLI command. Profile content is treated as Principal Context — only an SHA-256 commitment of the file is written to `provenance.source_commitments`, the original YAML is not persisted.

### Charter Issuer

The party that creates, reviews, signs, and publishes a Charter. The issuer can be the principal, a delegated service, an enterprise admin, or an upstream agent. In the v0 demo the principal and issuer are usually the same human for simplicity.

### Agent Operator

The party that owns or runs the underlying agent implementation and publishes capability metadata such as an Agent Card. The operator is not necessarily the principal. One operated agent can serve many principals through different Charters.

### Owner

An ambiguous informal term. Avoid using it as a canonical role unless the surrounding protocol already defines it. In this project, prefer Principal, Charter Issuer, or Agent Operator.

### Charter

A signed, queryable work contract for one `principal x agent` relationship. A Charter is not an Agent Card field. It is a separate relationship artifact issued for the principal-agent relationship, and it may change when the principal context changes.

### Charter Instance

One concrete Charter object bound to a specific principal and a specific agent. It is not bound to a model, an agent class, a framework, or a single task. Per-task Authorization can reference a Charter Instance, but does not replace it.

### Clause

A versioned natural-language policy statement inside a Charter. Clauses are the primary v0 policy surface because agent tasks are too open-ended to pre-enumerate with a fixed action/resource taxonomy.

### Public Charter

The signed artifact that other agents can fetch and evaluate. It contains the relationship binding, minimal public principal and issuer metadata, clauses, decision schema, validity window, and signatures. It must not expose raw Principal Context.

### Self-Attesting Charter

A Charter that carries the issuer's public key inline in its own `provenance.issuer_public_key` field, so a calling agent can verify the issuer signature using only the Charter JSON itself plus HTTPS-level trust in the hosting URL. v0 uses this model to avoid a separate JWKS endpoint or external PKI. Future versions can layer JWKS, public-key fingerprint pinning, or a transparency log on top.

### Provenance

Metadata describing where a Charter was derived from without publishing the raw source material. In v0 this should be source type, timestamp, summary, and optional hash or commitment.

### Charter Lifecycle

The validity and replacement state of a Charter Instance. v0 uses manual re-issue with short validity windows. An expired or superseded Charter should return `needs_approval` and ask for a fresh Charter or explicit authorization. A revoked Charter or invalid signature should return `incompatible`.

### Agent Card

A capability declaration that says what an agent can technically do. Agent Card describes an agent in isolation; Charter describes the principal-agent relationship.

### Authority Layer

The missing middle layer between capability and per-task authorization. It answers: "This agent acts for whom, under what continuing constraints?"

### Authorization

Approval for a specific task or transaction. In the project narrative, AP2 Mandate is the comparable per-task authorization layer, while Charter is the continuing authority layer.

### Projection

The process of turning principal context, such as profile answers or memory, into a draft Charter. Projection is expected to be reviewed by the principal or a delegated Charter Issuer before signing and publishing.

### Charter Discovery

The process of resolving a `(principal_id, agent_id)` pair to a `charter_url`. v0 uses a directory service exposed at `charter.dev/api/lookup` and queried through the SDK helper `resolve_charter_url(principal_id, agent_id)`. Calling agents that already know the `charter_url` (for example via the target agent's Agent Card or identity header) skip this step. Self-hosted Charters can also be discovered through `.well-known/charter/{agent_id}` on the principal's own domain.

### Compatibility Check

A pre-flight check run by a calling agent before delegating to a target agent. It is LLM-first and schema-bound: the calling agent's own LLM compares the intended task against verified Charter clauses, then returns one of three machine-readable decisions: `allow`, `needs_approval`, or `incompatible`, with structured matched-clause entries and a short reason.

### Local Decision

The per-clause decision produced by mapping a single clause's `type` field through the protocol constant `TYPE_TO_DECISION`. The mapping is fixed by the protocol: `scope → allow`, `out_of_scope → incompatible`, `approval_required → needs_approval`, `operational_limit → needs_approval`, `style → allow`, `data_handling → needs_approval`. The LLM only judges whether each clause is hit by the intended task; it does not invent the local decision.

### Aggregate Decision

The final Compatibility Check verdict produced by combining all local decisions of matched clauses. The aggregation rule is strict precedence: `incompatible > needs_approval > allow`. If no clause matches, or if every matched clause has confidence below 0.5, the aggregate decision defaults to `needs_approval` as a conservative fallback.

### Applied Clause

The clause (or clauses) whose local decision determined the final aggregate decision. The verdict's `matched_clauses` array marks each entry with `applied: true|false` so a caller can immediately see which clause forced the outcome.

### Scope Rewrite

A Charter-guided rewrite of an incompatible or approval-bound task into a nearby task that fits the target Charter. Scope Rewrite is the purpose of `propose_within_scope`; it keeps delegation moving without hiding the original incompatibility.

### Loopback Verification

The full design where `propose_within_scope` automatically feeds its generated rewrite back through `check_compatibility` and retries with prompt-evolution and temperature annealing until the rewrite is accepted or a maximum attempt count is reached. **Not implemented in v0** — v0 issues a single rewrite and returns it without loopback. The full design is documented as a v0+ future direction.

### Delegation Gate

The v0 enforcement point. A calling agent treats the Compatibility Check result as a gate before delegating work. This can block delegation in compliant clients, but it does not prevent a malicious or unintegrated agent from bypassing the protocol.

### Capability-Boundary Enforcement

A stronger future enforcement point where the actual resource boundary, such as a database gateway, payment gateway, file system, or tool runtime, requires Charter-compatible approval before executing the operation.

### Charter Chain

A delegation chain where each downstream agent's Charter must be a subset of, or stricter than, the upstream principal's Charter. This supports scope attenuation across agent-as-principal workflows.

**v0 distinction.** Charter Chain is **not** the same as a calling agent fetching a single target Charter. The v0 demo path is single-Charter: the calling agent fetches one `principal × target_agent` Charter and runs Compatibility Check against it. Multi-hop chain enforcement requires checking both upstream authorization and downstream attenuation per delegation hop, and is out of scope for v0.

## Domain Relationships

- Capability says what an agent can do technically.
- Charter says what the agent may do under a principal.
- Authorization says what is approved for this specific task.
- Charter policy is represented as versioned natural-language clauses, not an infinite set of hard-coded fields.
- A Charter is a public work contract, not a public dump of Principal Context.
- Provenance may prove or summarize sources, but it should not reveal raw memory or profile data.
- Compatibility judgment is made by the calling agent's own LLM by default, not a central external judge API.
- `propose_within_scope` turns a failed or blocked delegation into a nearby in-scope task proposal.
- Charter validity is short-lived and manually renewed in v0.
- Expired or superseded Charters degrade to `needs_approval`; revoked Charters and invalid signatures are `incompatible`.
- The binding grain of a Charter is `principal x agent`.
- Principal answers "on whose behalf?"
- Charter Issuer answers "who created and signed this Charter?"
- Agent Operator answers "who runs this underlying agent?"
- The calling agent fetches and checks a Charter before delegating work.
- The target agent does not need code changes in v0; the Charter is associated externally through a URL.
- A single underlying agent can have multiple Charters for different principals.

## v0 Scope

- Accept a Profile YAML and project it into a Charter draft via one LLM call.
- Sign the Charter with the issuer's Ed25519 key and embed the corresponding public key inline (Self-Attesting Charter).
- Publish a Public Charter at a stable URL on `charter.dev/{principal}/{agent}`.
- Provide a `charter issue` CLI command that runs projection, signing, and publishing in one shot from a profile.yaml.
- Provide an MCP server with three tools: `fetch_charter(charter_url)`, `check_compatibility(charter, intended_task)`, and `propose_within_scope(charter, task, failed_verdict)`.
- Encode clause local decisions via the protocol constant `TYPE_TO_DECISION` mapping; aggregate using strict precedence `incompatible > needs_approval > allow`.
- Return structured `matched_clauses` with `{id, local_decision, applied, confidence, reason}` so verdicts are auditable.
- Implement a Delegation Gate for compliant calling agents.
- Demo multiple Charter Instances for the same underlying agent under different principals (Alice and Bob, both binding to `research_agent_v1`).

## Out Of Scope For v0

- Full Mem0 or Letta synchronization.
- Automatic re-projection when Principal Context changes.
- Multi-hop Charter chains as the main demo path.
- Framework SDK integration.
- AP2 payment execution.
- Enterprise HRIS integration.
- Cryptographic enforcement beyond basic Ed25519 signing and self-attesting verification.
- Capability-Boundary Enforcement at real resource gateways.
- Fine-grained private clauses or selective disclosure.
- Loopback Verification and retry logic inside `propose_within_scope` (single-shot rewrite in v0).
- Edge-case fallback policies (zero-clause match, low-confidence match) beyond the basic conservative default of `needs_approval`.
- `service_attestation` as a second signature layer on Charters.
- JWKS endpoint or external public-key distribution; v0 relies on Self-Attesting Charter + HTTPS.
- Charter Discovery directory service beyond a stub `charter.dev/api/lookup` lookup; calling agents in the demo know the URL directly.
- Self-hosted `.well-known/charter/{agent_id}` deployment mode (schema supports it, demo does not exercise it).
- CLI commands beyond `charter issue` and `charter inspect` (revoke and renew are designed but not implemented).
