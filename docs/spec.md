# Charter Protocol Specification (v0.1)

*What this doc is: the implementer's reference for building a Charter-compatible system. It defines the data shapes, decision rules, lifecycle states, signing model, and MCP tool contracts. For the "why" behind these choices, see [`design.md`](./design.md). For canonical terminology, see [`../CONTEXT.md`](../CONTEXT.md). For the implemented and planned surface, see [`../ROADMAP.md`](../ROADMAP.md).*

---

## 1. Overview

Charter is the Authority layer in a three-layer separation: **Capability** (what an agent can technically do, e.g. Agent Card), **Authority** (who an agent acts for and under what continuing constraints), and **Authorization** (what is approved for one specific task, e.g. AP2 Mandate). Charter occupies the middle layer that today's agent protocols leave empty.

A Charter binds at the grain of a `principal x agent` relationship pair. The same underlying agent serving two principals carries two independent Charters; the calling agent fetches and evaluates the one corresponding to the relationship it is about to use.

---

## 2. Roles

| Role | Definition |
|---|---|
| **Principal** | The person, organization, or upstream agent on whose behalf a target agent is acting. Supplies the context that determines scope, refusals, operating limits, and escalation. |
| **Charter Issuer** | The party that creates, reviews, signs, and publishes the Charter. May be the principal themselves, a delegated service, an enterprise admin, or an upstream agent. |
| **Agent Operator** | The party that runs the underlying agent implementation and publishes capability metadata (e.g. an Agent Card). One operated agent can serve many principals through different Charters. |

The informal term *owner* should be avoided; it conflates these three roles.

---

## 3. Charter JSON Schema (v0.1)

A Public Charter is a single JSON document. The full shape:

```json
{
  "version": "0.1",
  "charter_id": "charter:<principal_id>:<agent_id>:<issued_date>",

  "binding": {
    "type": "principal_agent",
    "principal_id": "string",
    "agent_id": "string"
  },

  "principal": {
    "type": "human | organization | agent",
    "id": "string",
    "role_summary": "string"
  },

  "issuer": {
    "type": "human | organization | service | agent",
    "id": "string",
    "relationship_to_principal": "self | delegated | admin | upstream"
  },

  "agent_operator": {
    "type": "service | individual",
    "id": "string",
    "agent_card_url": "string | null"
  },

  "principal_chain": [],

  "visibility": {
    "charter": "public",
    "raw_principal_context": "private",
    "private_clauses": "not_supported_in_v0"
  },

  "summary": {
    "plain_language": "string"
  },

  "clauses": [
    {
      "id": "string (e.g. C-001)",
      "type": "scope | out_of_scope | approval_required | operational_limit | style | data_handling",
      "text": "string"
    }
  ],

  "decision_schema": {
    "decision": "allow | needs_approval | incompatible",
    "matched_clauses": [
      {
        "id": "string",
        "local_decision": "allow | needs_approval | incompatible",
        "applied": "bool",
        "confidence": "float in [0, 1]",
        "reason": "string"
      }
    ],
    "reason": "string",
    "rewrite_available": "bool"
  },

  "lifecycle": {
    "issued_at": "ISO-8601 timestamp",
    "valid_until": "ISO-8601 timestamp",
    "status": "active | expired | revoked | superseded",
    "revoked_at": "ISO-8601 timestamp | null",
    "replaces": "charter_id | null",
    "replaced_by": "charter_id | null"
  },

  "provenance": {
    "issuer_public_key": "ed25519:<base64-DER-public-key>",
    "issuer_signature": "ed25519:<base64-signature>",
    "source_commitments": [
      {
        "type": "string (e.g. profile_yaml)",
        "description": "string",
        "content_hash": "sha256:<hex>"
      }
    ],
    "generated_at": "ISO-8601 timestamp"
  }
}
```

### Field notes

- **`charter_id`** is the only canonical identifier; it must be unique per Charter instance.
- **`binding.agent_id`** is the single source of truth for the bound agent. There is no top-level `agent_id`.
- **`principal_chain`** is reserved for Charter Chain attenuation (deferred — see [`design.md`](./design.md)). In v0.1 it is always an empty array.
- **`visibility.private_clauses`** must be `"not_supported_in_v0"`. Selective disclosure is a future-version concern.
- **`summary.plain_language`** is a human-readable summary; it is informational and not used in decision aggregation.
- **`decision_schema`** documents the shape of a Compatibility Check verdict, not the verdict itself. It is metadata that lets a caller know what to expect from `aggregate_verdict`.
- **`provenance.source_commitments[].content_hash`** is an opaque commitment to source material (typically the Profile YAML). The raw source must not be published.

---

## 4. Clause Types and `TYPE_TO_DECISION`

Every clause has a `type` field drawn from a closed set. The protocol fixes a constant mapping from clause type to **Local Decision** — the decision that clause contributes if a calling agent's LLM judges it to be hit by an intended task.

```python
TYPE_TO_DECISION = {
    "scope":             "allow",
    "out_of_scope":      "incompatible",
    "approval_required": "needs_approval",
    "operational_limit": "needs_approval",
    "style":             "allow",
    "data_handling":     "needs_approval",
}
```

| `clause.type` | Local decision | Meaning |
|---|---|---|
| `scope` | `allow` | Task type the agent is positively chartered to perform for this principal. |
| `out_of_scope` | `incompatible` | Task type explicitly excluded for this principal-agent pair. |
| `approval_required` | `needs_approval` | Allowed only after explicit principal approval per session/task. |
| `operational_limit` | `needs_approval` | Hours, budgets, frequency, geography; out-of-bound execution needs approval. |
| `style` | `allow` | Soft output preferences (format, language, citation). |
| `data_handling` | `needs_approval` | Sensitive data classes; explicit approval required before contact. |

The LLM is responsible for the **fuzzy** judgment ("does this clause hit this task?"); the protocol is responsible for the **deterministic** judgment ("given hits, what is the verdict?"). Implementations MUST NOT let the LLM freely emit a local decision; the local decision is derived mechanically from `clause.type`.

---

## 5. Aggregation Rule

A Compatibility Check verdict is produced by aggregating all local decisions of clauses the LLM marked as hit (and whose `confidence >= 0.5`).

The aggregation rule is strict precedence:

```
incompatible  >  needs_approval  >  allow
```

Reference implementation:

```python
def aggregate(local_decisions: list[str]) -> str:
    if "incompatible" in local_decisions:
        return "incompatible"
    if "needs_approval" in local_decisions:
        return "needs_approval"
    return "allow"
```

This rule is single-pass, deterministic, monotonic, and unit-testable. Any clause that says "stop" stops the verdict — a Charter is a boundary declaration, not an allow-list, so stacked constraints are AND-equivalent rather than OR-equivalent.

### Fallback rules

| Situation | Aggregate decision |
|---|---|
| No clause matches | `needs_approval` (conservative default — Charter is a closed-world constraint) |
| All matched clauses have `confidence < 0.5` | `needs_approval` (low LLM confidence degrades) |
| Lifecycle is `expired` or `superseded` | `needs_approval`, request fresh Charter |
| Lifecycle is `revoked` or signature verification fails | `incompatible`, do not delegate |

### Applied Clause

The verdict's `matched_clauses` array marks each entry with `applied: true|false`. An entry is applied when its local decision matches the aggregate decision (or when, for `allow` aggregates, no negative clause overrode it). Multiple clauses can be `applied: true` simultaneously. This makes the verdict directly auditable: callers can trace which clause forced which outcome.

---

## 6. Lifecycle

Every Charter carries a `lifecycle` block. Calling agents MUST honor the state machine before running semantic checks.

| State | Caller behavior |
|---|---|
| `active` and current time `<= valid_until` | Run Compatibility Check normally. |
| `expired` or current time `> valid_until` | Return `needs_approval`. Request a fresh Charter or explicit per-task authorization. |
| `revoked` | Return `incompatible`. Do not delegate. |
| `superseded` | Fetch the Charter referenced by `lifecycle.replaced_by` and re-run the check against it. |

v0 uses **manual re-issue with short validity windows** (recommended default: 30 days). No automatic re-projection. When principal context changes, the issuer regenerates, reviews, signs, and republishes. The old Charter is either marked `superseded` (with `replaced_by` pointing to the new `charter_id`) or `revoked`.

Expired-but-served Charters: a server MAY return an expired Charter as long as the `lifecycle.status` and `valid_until` fields accurately reflect that state. The caller's gate is what enforces the degradation.

---

## 7. Self-Attesting Charter Signing Model

v0 uses a **Self-Attesting Charter**: the Charter's own `provenance.issuer_public_key` field carries the public key needed to verify `provenance.issuer_signature`. A calling agent fetches the Charter over HTTPS and can verify the signature without any additional endpoint or external PKI lookup.

### Trust chain

```
HTTPS (TLS / CA system)
    ↓ trusts the domain hosting the Charter
Server returns Charter JSON containing issuer_public_key + issuer_signature
    ↓
Calling agent verifies issuer_signature using issuer_public_key
```

### What is signed

`issuer_signature` covers the canonical serialization of the entire Charter JSON with the `provenance.issuer_signature` field itself excluded. Implementations should:

1. Build the Charter object with `issuer_signature` set to `null` (or absent).
2. Canonicalize: serialize as UTF-8 JSON with sorted keys and stable number formatting.
3. Sign the resulting bytes with the issuer's Ed25519 private key.
4. Write the resulting signature into `provenance.issuer_signature` as `ed25519:<base64>`.

Verification reverses this process: strip the signature field, canonicalize, verify with the embedded public key.

### Algorithm

- **v0:** Ed25519 only.
- Public keys are encoded as `ed25519:<base64-DER>`.
- Signatures are encoded as `ed25519:<base64-raw>`.

Future versions MAY add additional algorithms or layer JWKS, key pinning, and a transparency log on top of this baseline. See [`design.md`](./design.md) for the rationale and deferred trust-model work.

---

## 8. Public Charter vs. Principal Context

The Charter is a public work contract. The material used to draft it is not.

| Content | Public in v0 | Why |
|---|---|---|
| `charter_id`, `binding` | Yes | Lets calling agents confirm which relationship they are checking. |
| Minimal `principal` identity | Yes | Enough role/ID surface to interpret the authority claim. |
| `issuer`, `agent_operator` | Yes | Tells callers who signed and who runs the agent. |
| `clauses[]`, `decision_schema` | Yes | The core input to Compatibility Check. |
| `lifecycle`, `provenance.issuer_signature`, `provenance.issuer_public_key` | Yes | Required to verify validity and integrity. |
| `provenance.source_commitments[]` (type + hash + description) | Yes | Proves derivation without exposing material. |
| Raw memory, conversation history, full profile | **No** | Principal Context — never on the public artifact. |
| Original source documents (CV, internal policy, etc.) | **No** | Only commitments (type + hash + summary) are published. |
| Private clauses | **Not supported in v0** | If the calling agent cannot read a clause, it cannot stably judge against it. Selective disclosure is deferred. |

A Profile YAML is treated as Principal Context. Only a SHA-256 commitment of the file appears in `provenance.source_commitments`; the original YAML is not persisted in or alongside the Public Charter.

---

## 9. MCP Tool Contracts

A Charter-compatible MCP server exposes a small, orthogonal tool surface.

### 9.1 `fetch_charter`

```python
charter = fetch_charter(charter_url: str) -> Charter
```

**Responsibility:** retrieve and verify a Charter. The server fetches the URL, verifies the signature using the embedded public key, and returns the parsed Charter object.

**Errors:** typed exceptions (or equivalent typed error responses) for the failure modes:

| Exception | When | How the caller should treat it |
|---|---|---|
| `CharterNotFoundError` | HTTP 404 or empty body | Treat as no Charter available; conservative escalation. |
| `CharterSignatureError` | Signature verification fails | `incompatible`. Do not delegate. |
| `CharterExpiredError` | `lifecycle.status == "expired"` or current time past `valid_until` | `needs_approval`. Request fresh Charter. |
| `CharterRevokedError` | `lifecycle.status == "revoked"` | `incompatible`. |
| `CharterSchemaError` | Schema validation fails | Refuse to use the Charter; report. |

### 9.2 `aggregate_verdict`

```python
verdict = aggregate_verdict(charter: Charter, hits: list[ClauseHit]) -> Verdict
```

**Responsibility:** apply the deterministic aggregation rule to a set of clause hits and produce a verdict matching `decision_schema`. This tool does NOT call an LLM. The calling agent's own LLM produces the `hits` input (per-clause hit/no-hit, confidence, reason); the server aggregates.

**Input** (each clause hit):

```python
{
  "id": "C-002",
  "hit": true,
  "confidence": 0.94,
  "reason": "Task explicitly requests marketing copy."
}
```

**Output** (Verdict):

```json
{
  "decision": "incompatible",
  "matched_clauses": [
    {
      "id": "C-002",
      "local_decision": "incompatible",
      "applied": true,
      "confidence": 0.94,
      "reason": "Task explicitly requests marketing copy."
    }
  ],
  "reason": "C-002 excludes marketing copy work under this Charter.",
  "rewrite_available": true
}
```

`rewrite_available` is a heuristic flag (e.g. there is at least one `scope` clause in the Charter, so a `propose_within_scope` call could plausibly succeed). It is informational, not a guarantee.

### 9.3 Inbox / Outbox helpers (delegation flow)

For inter-agent delegation flows the server exposes a thin envelope helper layer:

- `delegate_task(target_principal_id, target_agent_id, intended_task, from_agent)` — wraps a task envelope, resolves the Charter URL, and writes to the target's inbox.
- `check_inbox()` — the target reads the most recent inbound task.
- `send_result(task_id, verdict, response_text, executed, execution_output, from_agent)` — the target posts its verdict and result.
- `read_outbox()` — the original caller reads the response.

These helpers do not change the protocol's decision semantics. They exist so a demo or integration test can exercise the gate end-to-end without bringing in a full message bus.

### 9.4 `propose_within_scope` (deferred to v0.6+)

```python
proposal = propose_within_scope(
    charter: Charter,
    intended_task: str,
    failed_verdict: Verdict,
) -> RewriteProposal | RewriteFailure
```

**Responsibility:** when Compatibility Check returns `incompatible` with `rewrite_available=true`, generate a nearby in-scope rewrite of the task.

**Output** (`RewriteProposal`):

```json
{
  "rewritten_task": "string",
  "why_in_scope": "string",
  "referenced_clauses": ["C-001", "C-002"],
  "remaining_approval_needed": false
}
```

In v0 (single-shot, deferred to v0.6) the server performs one LLM call and returns the rewrite without verifying that the rewrite actually passes Compatibility Check. The full **Loopback Verification** design (feed the rewrite back through the check, anneal temperature, retry on failure, return `RewriteFailure` on exhaustion) is documented in [`design.md`](./design.md) and listed in [`../ROADMAP.md`](../ROADMAP.md) under v0.6.

---

## 10. Charter Discovery

A calling agent that has not been handed a `charter_url` directly must resolve `(principal_id, agent_id)` to one. The protocol supports two complementary shapes; v0.5 of this repository implements both.

### 10.1 SaaS-hosted URL shape

```
{base}/{principal_id}/{agent_id}
```

Examples:

```
https://charter.dev/alice@acme.com/research_agent_v1
https://charter.example.org/bob@startup.io/research_agent_v1
```

The server returns the signed Charter JSON at this URL. An optional lookup endpoint at `{base}/api/lookup?principal=...&agent=...` returns the canonical `charter_url`, supporting future deployments where the path layout differs from the canonical form.

### 10.2 Self-hosted `.well-known` shape

```
https://{principal_domain}/.well-known/charter/{agent_id}
```

Example:

```
https://alice.example.com/.well-known/charter/research_agent_v1
```

This mirrors the Web Bot Auth and `robots.txt` conventions: principals publish their Charters on their own domain rather than relying on a hosted service. In this mode `principal_id` is implied by the host and is not part of the path.

### 10.3 SDK helper

```python
charter_url = resolve_charter_url(principal_id, agent_id)
```

The helper consults a local directory file (`data/charters/index.json` in the v0.5 reference implementation) plus configurable resolver hooks for SaaS and self-hosted lookups. A calling agent that already has a `charter_url` (for example from a target agent's Agent Card or signed identity header) skips discovery entirely.

---

## Footer

The original combined hackathon-era source for this protocol is preserved at [`./legacy/hackathon-design.md`](./legacy/hackathon-design.md) for historical reference. That document mixes specification, rationale, demo scripts, and pitch material; this file is the clean specification slice extracted from it.
