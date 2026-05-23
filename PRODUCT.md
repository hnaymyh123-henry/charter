# Charter

> **The Authority layer for the agent economy.** A signed, queryable work
> contract that tells calling agents what an underlying agent is allowed
> and willing to do for a given principal — *before* the agent is asked
> to do it.

This is the single source of truth for what Charter is, why it exists,
how it works, what's shipped, and where it's going. For an install
guide see [`README.md`](README.md); for canonical terminology see
[`CONTEXT.md`](CONTEXT.md); for the prioritized iteration plan see
[`ROADMAP.md`](ROADMAP.md).

> **中文读者**:本文有一份对应的中文版 [`PRODUCT.zh.md`](PRODUCT.zh.md)
> ,内容跟英文版保持同步。英文版是 canonical;中文版为方便阅读保留。

---

## Contents

- [1. What Charter Is](#1-what-charter-is)
- [2. Why It Exists](#2-why-it-exists)
- [3. Roles](#3-roles)
- [4. How It Works (Protocol)](#4-how-it-works-protocol)
  - [4.1 Charter JSON Schema](#41-charter-json-schema)
  - [4.2 Clause Types and Local Decisions](#42-clause-types-and-local-decisions)
  - [4.3 Aggregation Rule](#43-aggregation-rule)
  - [4.4 Lifecycle](#44-lifecycle)
  - [4.5 Self-Attesting Signing](#45-self-attesting-signing)
  - [4.6 Public Charter vs. Principal Context](#46-public-charter-vs-principal-context)
  - [4.7 MCP Tool Surface](#47-mcp-tool-surface)
  - [4.8 Discovery](#48-discovery)
  - [4.9 Charter Chain Attenuation](#49-charter-chain-attenuation)
- [5. Design Rationale](#5-design-rationale)
- [6. Anti-Goals: What Charter Is Not](#6-anti-goals-what-charter-is-not)
- [7. Current State](#7-current-state)
  - [7.1 v0.5 — Project Hygiene](#71-v05--project-hygiene)
  - [7.2 v0.6 — Protocol Completion](#72-v06--protocol-completion)
  - [7.3 v0.7 — Charter Chain + Adapter + Deploy](#73-v07--charter-chain--adapter--deploy)
- [8. Roadmap](#8-roadmap)
  - [8.1 v0.8 — Trust Model Upgrade (planning)](#81-v08--trust-model-upgrade-planning)
  - [8.2 Beyond v0.8 (deferred backlog)](#82-beyond-v08-deferred-backlog)
- [9. References](#9-references)

---

## 1. What Charter Is

Charter is a signed JSON contract that says **"this specific agent, acting
for this specific principal, is allowed and willing to do these things —
and refuses these others — under these continuing constraints."**

It sits in the middle of a three-layer separation that today's agent
protocols leave incomplete:

| Layer | What it answers | Existing protocols |
|---|---|---|
| **Capability** | What can the agent technically do? | Agent Card (A2A) |
| **Authority** | Who does the agent act for, under what continuing constraints? | **Charter** |
| **Authorization** | Was this specific task approved? | AP2 Mandate |

Without Charter, agent ecosystems have capability + per-task authorization
but no continuing relational layer. Result: calling agents either trust
target agents blanket (unsafe) or re-ask the user per task (unusable).

**A Charter binds at the grain of a `principal × agent` relationship.**
The same underlying agent serving Alice and Bob carries two independent
Charters with different scope, exclusions, hours, budgets, and escalation
rules. The calling agent fetches the one matching the relationship it's
about to invoke and runs a Compatibility Check before delegating.

---

## 2. Why It Exists

Cloud-side IAM solved the same three-layer separation a decade ago for
service identity. The agent economy is rebuilding the bottom and top of
that stack — Capability via Agent Card, Authorization via AP2 — but
nothing in the middle.

The concrete failure mode this creates: an agent that is technically
capable of `DROP TABLE production` has no protocol-level way to declare
"…but not when I'm working for Alice the accountant" without burying that
constraint inside the agent operator's own runtime. There's no signed,
fetchable, third-party-verifiable statement of continuing authority.
Charter is that statement.

The full structural argument — why this can't be folded into Agent Card,
why this isn't a Constitutional-AI concern, why voluntary protocols still
work — lives in [§5 Design Rationale](#5-design-rationale).

---

## 3. Roles

| Role | Definition |
|---|---|
| **Principal** | The person, organization, or upstream agent on whose behalf a target agent is acting. Supplies the context that determines scope, refusals, operating limits, and escalation. |
| **Charter Issuer** | The party that creates, reviews, signs, and publishes the Charter. May be the principal themselves, a delegated service, an enterprise admin, or an upstream agent. |
| **Agent Operator** | The party that runs the underlying agent implementation and publishes capability metadata (e.g. an Agent Card). One operated agent can serve many principals through different Charters. |

The informal term *owner* is deliberately avoided — it conflates these
three roles, which need to stay distinct.

---

## 4. How It Works (Protocol)

This section is the implementer's reference. If you are building a
Charter-compatible system from scratch, everything you need is here.

### 4.1 Charter JSON Schema

A Public Charter is a single JSON document.

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
        "reason": "string",
        "source_charter_id": "string | null"
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
  },

  "parent_charter_url": "string | null",
  "attenuation_proof": {
    "parent_charter_id": "string",
    "attenuates": { "<child_clause_id>": ["<parent_clause_id>", ...] }
  }
}
```

**Field notes:**

- `charter_id` is the only canonical identifier; unique per Charter instance.
- `binding.agent_id` is the single source of truth for the bound agent. No top-level `agent_id`.
- `principal_chain` is reserved; in v0.1 always an empty array.
- `visibility.private_clauses` accepts `"not_supported_in_v0"` (pre-ADR-011 default) or `"redaction_v1"` (ADR-011 path 1 — per-span redaction with SD-JWT-style commitments, see §4.6). Whole-clause hiding remains deferred to path 2.
- `clauses[].private_fields` is optional. When set, each entry carries `{span_start, span_end, disclosure_hash}` pointing into the redacted clause text. The signature commits to the hash, never the plaintext; Charters that omit `private_fields` entirely retain byte-for-byte signing compatibility with v0.x.
- `summary.plain_language` is informational; not used in decision aggregation.
- `decision_schema` documents the *shape* of a Compatibility Check verdict, not the verdict itself.
- `provenance.source_commitments[].content_hash` is opaque to the public artifact; raw sources are never published.
- `parent_charter_url` + `attenuation_proof` are populated only on child Charters in a chain (v0.7+). Root Charters set both to `null`.
- `source_charter_id` in `matched_clauses` is populated only by `aggregate_verdict_chain` (v0.7+) so callers can see which Charter in a chain forced the outcome. Single-Charter aggregations leave it `null`.

### 4.2 Clause Types and Local Decisions

Every clause has a `type` from a closed set. The protocol fixes a
constant mapping from clause type to **Local Decision** — the decision
that clause contributes if a calling agent's LLM judges it to be hit by
an intended task.

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
| `data_handling` | `needs_approval` | Sensitive data classes; explicit approval before contact. |

**The LLM does the fuzzy judgment** (does this clause apply to this
task?); **the protocol does the deterministic judgment** (given hits,
what is the verdict?). Implementations MUST NOT let the LLM freely emit
a local decision; it is derived mechanically from `clause.type`.

### 4.3 Aggregation Rule

A Compatibility Check verdict is produced by aggregating local decisions
of clauses the LLM marked hit (with `confidence >= 0.5`).

Strict precedence:

```
incompatible  >  needs_approval  >  allow
```

```python
def aggregate(local_decisions: list[str]) -> str:
    if "incompatible" in local_decisions:
        return "incompatible"
    if "needs_approval" in local_decisions:
        return "needs_approval"
    return "allow"
```

Single-pass, deterministic, monotonic, unit-testable. Any clause that
says "stop" stops the verdict — a Charter is a boundary declaration, not
an allow-list, so stacked constraints are AND-equivalent.

**Fallback rules:**

| Situation | Aggregate decision |
|---|---|
| No clause matches | `needs_approval` (conservative; Charter is closed-world) |
| All matched clauses have `confidence < 0.5` | `needs_approval` (low LLM confidence degrades) |
| Lifecycle is `expired` or `superseded` | `needs_approval`, request fresh Charter |
| Lifecycle is `revoked` or signature invalid | `incompatible`, do not delegate |

**Applied Clause:** the verdict marks each `matched_clauses` entry with
`applied: true | false`. An entry is applied when its local decision
matches the aggregate. Multiple clauses can be applied simultaneously.
This makes verdicts directly auditable — callers can trace which clause
forced which outcome.

### 4.4 Lifecycle

| State | Caller behavior |
|---|---|
| `active` and `now <= valid_until` | Run Compatibility Check normally. |
| `expired` or `now > valid_until` | Return `needs_approval`. Request fresh Charter. |
| `revoked` | Return `incompatible`. Do not delegate. |
| `superseded` | Fetch the Charter at `lifecycle.replaced_by` and re-run. |

v0 uses **manual re-issue with short validity windows** (default 30
days). No automatic re-projection. When principal context changes the
issuer regenerates, reviews, signs, republishes; the old Charter is
either `superseded` (with `replaced_by` pointing at the new
`charter_id`) or `revoked`.

A server MAY return an expired Charter as long as `lifecycle.status` and
`valid_until` accurately reflect that state. The caller's gate enforces
the degradation.

**Client revocation visibility (v0.9 B1.3).** A naive caller that
caches a fetched Charter never learns that the issuer revoked it until
the cache TTL expires — hours, days, sometimes longer. v0.9 closes
that gap with two cooperating surfaces:

- Every Charter response (`GET /{principal}/{agent}`,
  `GET /.well-known/charter/{agent_id}`, `GET /api/lookup`) carries
  `Cache-Control: max-age=300, must-revalidate`. The TTL is operator-
  tunable via the `CHARTER_CACHE_TTL` env var; setting it to `0`
  disables the header for callers that want to manage their own
  freshness.
- `GET /transparency/revoked?since=<seq>` streams an `application/
  x-ndjson` feed of `{charter_id, principal_id, agent_id, revoked_at,
  seq}` rows derived live from the transparency log (no separate
  revocation file — see ADR-007). Clients pass the highest `seq` they
  have already consumed and get back only newer entries; non-integer
  or negative `since` returns 400 (fail-closed cursor).

The reference SDK exposes two helpers on top of those:
`charter.revocation.subscribe_revocations(origin, since,
poll_interval=60)` is an async generator that yields each new
`RevocationEntry`; `charter.revocation.RevocationAwareCache` wraps a
`dict[charter_id, Charter]` and, when used as an async context
manager, runs a background polling task that `pop`s any cached
Charter whose id arrives in the feed.

WebSub / WebHook push-mode delivery is deliberately deferred — pull-
mode is enough to make the security claim "callers can find out about
a revoke within `poll_interval + CHARTER_CACHE_TTL`" without forcing
every issuer to host a webhook fan-out.

### 4.5 Self-Attesting Signing

v0 uses a **Self-Attesting Charter**: the Charter's own
`provenance.issuer_public_key` field carries the public key needed to
verify `provenance.issuer_signature`. One fetch over HTTPS; no JWKS
round-trip, no DID resolution, no external PKI.

**Trust chain:**

```
HTTPS (TLS / CA system)
    ↓ trusts the domain hosting the Charter
Server returns Charter JSON containing issuer_public_key + issuer_signature
    ↓
Calling agent verifies issuer_signature using issuer_public_key
```

**What is signed:** `issuer_signature` covers the canonical
serialization of the Charter JSON with the `issuer_signature` field
itself excluded. Build the object with `issuer_signature` empty,
canonicalize (UTF-8 JSON, sorted keys, stable number format), sign the
bytes with the issuer's Ed25519 private key, write the resulting
signature back into `provenance.issuer_signature` as `ed25519:<base64>`.

**Algorithm:** Ed25519 only in v0.

**Private keys at rest** (v0.6+): encrypted with `BestAvailableEncryption`
when `CHARTER_KEY_PASSPHRASE` is set; plaintext PEM + loud WARN log
otherwise. Legacy plaintext keys remain loadable for backward compat.

**Known limits of v0** (addressed in v0.8 — see [§8.1](#81-v08--trust-model-upgrade-planning)):

- **TOFU on first fetch.** First time a calling agent sees a `charter_url`, no way to independently verify the embedded public key belongs to the stated principal. Trust collapses onto HTTPS.
- **Weak key-rotation discovery.** Rotated keys travel inline with the new Charter; no fingerprint-pinning to detect surprise rotations.
- **No defense against host compromise.** Compromised host can sign and serve arbitrary Charters.

### 4.6 Public Charter vs. Principal Context

The Charter is a public work contract. The material used to draft it is
not.

| Content | Public? | Why |
|---|---|---|
| `charter_id`, `binding` | Yes | Confirms which relationship the caller is checking. |
| Minimal `principal` identity | Yes | Enough role/ID surface to interpret authority. |
| `issuer`, `agent_operator` | Yes | Tells callers who signed and who runs the agent. |
| `clauses[]`, `decision_schema` | Yes | Core input to Compatibility Check. |
| `lifecycle`, signature, public key | Yes | Required to verify validity and integrity. |
| `provenance.source_commitments[]` (type + hash + description) | Yes | Proves derivation without exposing material. |
| Raw memory, conversation history, full profile | **No** | Principal Context — never on the public artifact. |
| Original source documents (CV, internal policy, etc.) | **No** | Only commitments are published. |
| Redacted clause spans (with `visibility.private_clauses == "redaction_v1"`) | **Hash only** | ADR-011 path 1 (v0.9). Clause structure, `type`, and surrounding text stay public so the grader LLM still judges hits; the sensitive span is replaced inline by `[REDACTED:<hash-prefix>]` and only its SHA-256(salt &#124;&#124; value) commitment enters the signed bytes. |
| Disclosure plaintexts (`data/disclosures/<charter>/<id>.json`) | **Behind bearer token** | Served from `GET /disclosures/{charter_id}/{disclosure_id}` only when the request carries `Authorization: Bearer <CHARTER_DISCLOSURE_TOKEN>`. All other requests get an indistinguishable 404, so disclosure ids cannot be enumerated by response shape. |
| Whole-clause hiding (selective disclosure of clause existence) | **Not in v0.9** | Reserved for ADR-011 path 2 (delegated grading) — see §8.2. |

A Profile YAML is treated as Principal Context. Only its SHA-256
commitment appears in `provenance.source_commitments`; the original YAML
is not persisted in or alongside the Public Charter.

**Redaction (path 1) caller workflow.** A calling agent that wants to
check "does this Charter touch customer Acme Corp?" runs the usual
grader LLM over the published clause text (with placeholders intact)
to estimate a hit. If it needs to confirm against a specific candidate
without ever fetching the plaintext, it calls
`charter.privacy.match_redacted(clause_text, "Acme Corp", disclosures)`
locally — a bool that returns True iff the SHA-256(salt &#124;&#124; "Acme Corp")
reproduces one of the in-clause commitments. The function deliberately
does not reveal which placeholder matched, so probing with many
candidates cannot enumerate the disclosure set.

### 4.7 MCP Tool Surface

A Charter-compatible MCP server exposes a small, orthogonal tool set.
The reference implementation in this repo ships **10 tools** (as of v0.7):

| # | Tool | LLM calls in tool | Purpose |
|---|---|---|---|
| 1 | `fetch_charter(charter_url)` | 0 | Pull + verify signature, return Charter + protocol hints. |
| 2 | `aggregate_verdict(charter, hits)` | 0 | Deterministically combine per-clause judgments into a Verdict. |
| 3 | `delegate_task(principal, agent, task)` | 0 | Calling agent → write task envelope to inbox. |
| 4 | `check_inbox()` | 0 | Worker agent → read pending task. |
| 5 | `send_result(task_id, verdict, ...)` | 0 | Worker agent → write reply to outbox. |
| 6 | `read_outbox()` | 0 | Calling agent → read worker's reply. |
| 7 | `propose_within_scope(url, task, failed_verdict)` | 1 | Single-shot rewrite of an incompatible task. |
| 8 | `propose_within_scope_verified(url, task, failed_verdict, max_attempts=3)` | up to 2N | Loopback-verified rewrite: anneals temperature, retries with feedback. |
| 9 | `fetch_charter_chain(charter_url, max_depth=5)` | 0 | Walk `parent_charter_url` to root, verify each hop's signature + attenuation. Returns chain root-first. |
| 10 | `aggregate_verdict_chain(chain, hits_per_charter)` | 0 | Apply precedence across all matched clauses from all Charters in a chain. Strictest wins. |

**Design principle:** the MCP server does NOT call an LLM by default.
The calling agent's own LLM does the fuzzy clause-hit judgment; the
server does deterministic aggregation. The exceptions are
`propose_within_scope` (single LLM call) and
`propose_within_scope_verified` (up to 2N LLM calls — the only
multi-LLM-call tool in the surface, documented as such).

**Typed errors** (raised by `_fetch_and_verify`, surfaced through the
MCP layer):

| Exception | When | Caller's response |
|---|---|---|
| `CharterNotFoundError` | HTTP 404 or unreachable | Conservative escalation. |
| `CharterSchemaError` | Body is not a valid Charter | Refuse to use; report. |
| `CharterSignatureError` | Signature did not verify | `incompatible`. Do not delegate. |
| `CharterRevokedError` | `lifecycle.status == "revoked"` | `incompatible`. |
| `CharterExpiredError` | `lifecycle.status in {"expired", "superseded"}` | `needs_approval`. |

### 4.8 Discovery

Two complementary URL shapes; v0.5+ implements both.

**SaaS-hosted (default):**

```
{base}/{principal_id}/{agent_id}
```

Example: `https://charter.dev/alice@acme.com/research_agent_v1`. An
`{base}/api/lookup?principal_id=...&agent_id=...` endpoint returns the
canonical `charter_url` for an arbitrary path layout.

**Self-hosted `.well-known` (Web Bot Auth pattern):**

```
https://{principal_domain}/.well-known/charter/{agent_id}
```

Example: `https://alice.example.com/.well-known/charter/research_agent_v1`.
Principals publish on their own domain. `principal_id` is implied by the
host. Gated server-side on the `CHARTER_SELF_HOSTED_PRINCIPAL` env var
in the reference implementation.

**SDK helper:**

```python
from charter.discovery import resolve_charter_url

url = resolve_charter_url("alice@acme.com", "research_agent_v1")
```

Consults the local `data/charters/index.json` (maintained automatically
on every `save_charter`) first; falls back to `{CHARTER_URL_BASE}/...`
composition. `strict=True` raises `CharterNotFoundError` instead of
falling back.

### 4.9 Charter Chain Attenuation

A Charter Chain is a sequence of Charters where each child claims to be
a *stricter subset* of its parent — the agent-as-principal case. A corp
issues a broad Charter to its assistant agent; the assistant, acting as
principal, issues a narrower Charter to a downstream research agent it
delegates to.

**Schema:** child Charters carry `parent_charter_url` + optional
`attenuation_proof.parent_charter_id`. Root Charters set both to
`null`.

**Verification rules (v0.7, string-based — semantic subset is deferred):**

1. Every `out_of_scope` clause in the parent is covered by some
   `out_of_scope` clause in the child (text equality OR child's text is
   a superstring). Child may add new exclusions.
2. Every `approval_required` clause is preserved or expanded (same rule).
3. Every `scope` clause in the child matches some `scope` clause in the
   parent by exact text equality. Child may have *fewer* scope clauses;
   that's the whole point of attenuation.
4. `attenuation_proof.parent_charter_id`, when present, matches the
   actual parent's `charter_id`.

**MCP tools:**

- `fetch_charter_chain` walks `parent_charter_url` to the root, verifies
  every hop's signature + lifecycle + attenuation. Cycle-safe (seen-set
  on `charter_id`), depth-bounded (default 5), returns chain root-first.
- `aggregate_verdict_chain` applies the same precedence rule across all
  matched clauses from all Charters in a chain. Strictest Charter wins.
  Each `matched_clauses` entry carries `source_charter_id` so the caller
  can see which Charter forced the outcome.

**Property the chain enforces:** the UNION of restrictions, not just the
parent's. A task forbidden by the child but not by the parent is still
blocked — that's what makes attenuation meaningful.

---

## 5. Design Rationale

This section is the *why*. The specification is normative; this is
explanatory. Skip on first read if you only need to implement.

### 5.1 Why a Principal / Authority Layer is the missing piece

Existing agent protocols all forward-declare one axis of the agent's
surface:

| Existing | Question it answers | Who declares | Grain |
|---|---|---|---|
| Agent Card (A2A) | What can I do? | Agent operator / framework | Per skill |
| Identity (Web Bot Auth) | Who am I? | Agent operator / CA | Per message |
| Resume / Reputation | What have I done? | Third parties | Aggregate |
| Mandate (AP2) | What did the user authorize for this task? | End user | Per transaction |

Stacked: capability + identity + history + per-task authorization. What's
missing: **what the agent is allowed or willing to do continuously, in
the principal context it currently belongs to.** Cloud-side IAM solved
this with three layers a decade ago — Charter fills the Authority middle.

### 5.2 Why `principal × agent` is the binding grain

| Candidate grain | Why rejected |
|---|---|
| **Model** | The same model serves countless agents and principals; permission semantics at this grain are useless. |
| **Agent class / Agent Card** | Describes only what the agent technically can do — cannot express "for whom". An Agent Card is intrinsic; Charter is relational. |
| **Single task** | This is the grain of AP2 Mandate. Charter is meant to outlive any single task. |
| **`principal × agent`** | Expresses the same agent serving different principals under different scope, refusals, hours, budgets, style, escalation. |

This grain also produces a critical property: the same underlying agent
can simultaneously hold many Charters — `alice × research_agent_v1`,
`bob × research_agent_v1`, `bookkeeper × ocr_agent_v1` — each an
independent Charter Instance. The calling agent always evaluates the
Charter for the specific relationship it is about to invoke.

### 5.3 Why versioned natural-language clauses, not fixed policy fields

The temptation, with IAM background, is to define a fixed taxonomy:
`actions`, `resources`, `data_classes`, etc. v0 rejects this. The agent
task space is open — today tax filing, tomorrow database cleanup,
next week helping another agent redesign a workflow. A pre-enumerated
taxonomy either bloats into an enterprise policy table nobody fills in,
or locks out task types nobody anticipated at schema time.

v0 chooses **versioned natural-language clauses + a structured verdict
contract + a protocol-constant type mapping** instead:

- Each clause has a stable `id` and a `type` from a small closed set.
- Clause `text` is natural language — as expressive as the underlying LM.
- `TYPE_TO_DECISION` fixes the mechanical contribution; the LLM only
  judges hits.

Forward-compatible with compiling common high-frequency clauses into
machine fields later; not foreclosed by them.

### 5.4 Why LLM-first, schema-bound checks

Compatibility Check is intentionally split:

- **LLM-first** because the input ("does this open-ended task hit this
  clause?") is exactly the fuzzy semantic judgment language models do
  well and rule systems do badly.
- **Schema-bound** because the output must be deterministic, auditable,
  composable. The LLM never returns a free-form verdict — it returns
  per-clause hits with confidence, and the protocol-side aggregator
  produces the verdict.

Two consequences:

1. **The calling agent's own LLM is the judge.** No central judging API.
   Operators don't need to trust an external service. The judgment lives
   where the rest of the calling agent's reasoning already lives.
2. **The aggregator is unit-testable.** `incompatible > needs_approval >
   allow` is three lines. Interesting logic moves into clause text
   (signed, reviewable) and into the LLM prompt (observable).

Stronger judges (a fine-tuned classifier) can swap in later without
changing the protocol surface.

### 5.5 Why Self-Attesting Charter + HTTPS as the v0 trust root

v0 ships the thinnest trust model that resists casual tampering. The
Charter carries its own public key inline. A calling agent fetches one
URL, verifies the signature, and is done.

Heavier alternatives were considered and explicitly deferred:

| Alternative | Why deferred |
|---|---|
| JWKS endpoint (`/.well-known/jwks.json`) | One more fetch; demos and integration tests have to explain it. |
| DIDs | Heavy infrastructure overhead for a protocol that does not yet need decentralized identity. |
| X.509 certificate chain | Disproportionate engineering effort vs. v0's threat surface. |
| `service_attestation` second-layer signature | Adds complexity without protecting against issuer-key compromise — the real risk at v0. |

The honest gaps — TOFU, weak rotation discovery, no host-compromise
defense — are documented in [§4.5](#45-self-attesting-signing) and
scheduled for v0.8.

### 5.6 Why Charter is a voluntary protocol

Charter is a **Delegation Gate**, not **Capability-Boundary Enforcement**.

| Layer | What it blocks | Who has to cooperate |
|---|---|---|
| **Delegation Gate** (v0) | A compliant calling agent's decision to send a task | The calling agent |
| **Capability-Boundary Enforcement** (deferred) | The actual resource operation (DB write, payment send, file delete) | The resource gateway |

Voluntary on purpose:

1. **Precedent.** `robots.txt` has been voluntary for 30 years and runs
   the indexable web. Cloudflare Web Bot Auth is voluntary. Voluntary
   protocols work in reputation-sensitive ecosystems.
2. **Iteration speed.** Resource-level enforcement requires integration
   with every gateway (DB, payments, filesystem, tools). Multi-quarter
   project. The Delegation Gate is a few hundred lines.
3. **Compatibility surface.** Voluntary protocols can be adopted
   incrementally. A capability-boundary system has to be in front of
   every operation or it adds nothing.

The honest claim Charter v0 makes: *"for calling agents that follow the
protocol, here is a stable, low-cost, auditable way to decide whether
to delegate."*

### 5.7 Why `propose_within_scope` is part of the protocol

A naive Charter that only returned `allow / needs_approval / incompatible`
would behave like a compliance refusal system. Useful but inert: calling
agents would either give up or hammer the gate.

`propose_within_scope` changes the protocol from a refusal list into a
**delegation router**. When the verdict is `incompatible`, the calling
agent can ask "given this Charter, what task could I legitimately
delegate instead?" — and get back a Charter-grounded rewrite with
referenced clauses.

Two consequences:

1. **Elevates the protocol from gate to coordinator.** Refusal alone tells
   the caller "not here." Rewrite plus refusal tells the caller "not
   this, but here." That's the difference between a permissions system
   and a marketplace-of-agents protocol.
2. **Puts pressure on clause design.** Clauses that produce useful
   rewrites tend to be specific (`accounting / tax / bookkeeping`)
   rather than vague (`work-related tasks`). The rewrite path is a
   forcing function for clause quality.

### 5.8 Why Charter Chain attenuation shipped in v0.7, not v0

A two-hop chain is the natural next demo: Alice issues a Charter to
BookkeeperBot, which in turn issues an attenuated Charter to an OCR
sub-agent. Each downstream Charter is a subset of the upstream.

Deferred from v0 to v0.7 for three reasons:

1. **The single-Charter check is the load-bearing primitive.** Chains
   compose single-Charter checks; chains can't meaningfully demo until
   the single-Charter case is solid.
2. **Attenuation semantics need clause-level support v0 didn't have.**
   A clean chain check requires either string-match subset rules
   (crude) or semantic subset reasoning (much harder). v0 ships the
   clause structure; v0.7 adds the chain logic. Semantic subset is
   v0.8+.
3. **The most common single-actor case is one human × one agent.** v0's
   whole purpose was to make the simple case land first.

v0.7 ships string-based chain verification. Semantic subset (LLM-driven)
is on the deferred backlog.

---

## 6. Anti-Goals: What Charter Is Not

Charter is repeatedly mistaken for things it deliberately is not.

**Not an Agent Card extension.** Agent Card describes an agent in
isolation. Charter describes a relationship. Different objects,
different data owners, different lifecycles, different cardinality.
Adding a `charter_url` reference to an Agent Card is fine; merging the
two artifacts is not.

**Not a replacement for Constitutional AI or alignment training.**
Constitutional AI shapes a model's behavior at training time. Charter
is a runtime, external, queryable layer about a specific
principal-agent relationship. Complementary — Constitutional AI says
what a model will and won't do regardless of context; Charter says what
*this deployment of that model, acting for this principal*, is supposed
to do.

**Not an enterprise IAM product.** Enterprise IAM (Okta, Microsoft
Entra, Google Cloud Agent Identity) targets corporate IT admins with
role-based top-down policy. Their data sources are HRIS and AD. Their
product surface is admin consoles. Charter targets individual principals
(or small orgs acting on behalf of one), uses the principal's own
context as the data source, projects clauses via LLM rather than admin
entry, has no console-based deployment story. Different markets;
different design choices.

**Not a guarantee against malicious agents.** A malicious calling agent
can ignore the protocol entirely. v0 acknowledges this and scopes its
claims accordingly — it improves the behavior of cooperating agents.
Hard enforcement is the Capability-Boundary Enforcement work on the
deferred backlog.

**Not a public dump of principal data.** The Public Charter is a work
contract, not a memory dump. Everything in
`provenance.source_commitments` is a commitment (type + summary + hash).
The original Profile YAML, memory, conversation history, and source
documents stay private. A reader of the Charter learns enough to decide
whether to delegate; they do not learn how the principal phrased their
preferences.

---

## 7. Current State

Three numbered releases have shipped on `main` so far. Each is a tagged
GitHub Release with notes; each closed its own milestone with one PR.

**Top-line stats (as of v0.7):**

- 18 modules in `charter/`
- 10 MCP tools
- 4 CLI commands (`issue`, `inspect`, `revoke`, `renew`)
- 158 tests, all 6 CI jobs green (`{py3.12, py3.13} × {ubuntu, macos, windows}`)
- `ruff` clean, `mypy --strict` clean

### 7.1 v0.5 — Project Hygiene

[Release notes](https://github.com/hnaymyh123-henry/charter/releases/tag/v0.5.0) · [PR #1](https://github.com/hnaymyh123-henry/charter/pull/1)

Graduated the hackathon prototype into a real, contributable project.

- **Apache 2.0 license** + full project metadata.
- **Typed exception hierarchy** (`charter.errors`): `CharterError` +
  `CharterNotFoundError` / `CharterSchemaError` / `CharterSignatureError`
  / `CharterExpiredError` / `CharterRevokedError`. Replaces
  `ValueError("CharterNotFoundError: ...")` prefix strings.
- **`/healthz`** liveness probe.
- **`/.well-known/charter/{agent_id}`** self-hosted route, gated on
  `CHARTER_SELF_HOSTED_PRINCIPAL`.
- **CI** on `{py3.12, py3.13} × {ubuntu, macos, windows}`. `ruff check`
  + `ruff format --check` + `mypy --strict` + `pytest`.
- **`Dockerfile`** (multi-stage, non-root, `/data` volume, `HEALTHCHECK`).
- **`fly.toml`** template + `.dockerignore`.
- **Doc split**: hackathon doc → `docs/spec.md` + `docs/design.md` +
  `docs/legacy/hackathon-design.md`. (These two have since been merged
  into this PRODUCT.md.)
- **35 tests** (was 14).

### 7.2 v0.6 — Protocol Completion

[Release notes](https://github.com/hnaymyh123-henry/charter/releases/tag/v0.6.0) · [PR #9](https://github.com/hnaymyh123-henry/charter/pull/9)

Shipped the v0-designed-but-unbuilt features so the implemented surface
matched the spec.

- **`propose_within_scope` MCP tool** — single-shot rewrite via Claude.
- **`propose_within_scope_verified`** — wraps single-shot with loopback
  verification: up to 3 attempts, temperature annealing 0.2 → 0.5 →
  0.8, prompt receives feedback on each retry. Returns
  `RewriteProposal` or `RewriteFailure(attempts=...)` with full history.
- **`charter revoke`** CLI — flips status, re-signs, so the revocation
  itself is verifiable. After this, `fetch_charter` raises
  `CharterRevokedError`.
- **`charter renew`** CLI — no LLM call. Same clauses + summary, new
  `charter_id`, fresh validity window. Predecessor goes to
  `data/charters/archive/` with `status=superseded`.
- **Charter Discovery** — `resolve_charter_url(principal, agent)` SDK
  helper + `data/charters/index.json` index file (auto-maintained by
  `save_charter`).
- **Structured logging** (`charter/_logging.py`) — human + JSON
  formatters. `CHARTER_LOG_FORMAT` env var. Every fetch outcome and CLI
  command emits one log line with
  `charter_id` / `principal_id` / `agent_id` / `outcome`.
- **Encrypted private keys at rest** — `CHARTER_KEY_PASSPHRASE` enables
  `BestAvailableEncryption`. Without it: plaintext PEM + WARN log on
  every write. v0 plaintext keys remain backward-compatible.
- **103 tests** (was 35).

### 7.3 v0.7 — Charter Chain + Adapter + Deploy

[Release notes](https://github.com/hnaymyh123-henry/charter/releases/tag/v0.7.0) · [PR #16](https://github.com/hnaymyh123-henry/charter/pull/16)

Charter Chain attenuation + first framework adapter + public-internet
deploy.

- **Chain schema** — `Charter.parent_charter_url` +
  `Charter.attenuation_proof` + `MatchedClause.source_charter_id`.
- **`verify_chain(child, parent)`** — string-based subset verifier
  (conservative, deterministic, zero LLM cost).
- **`fetch_charter_chain`** MCP tool — walks `parent_charter_url`,
  verifies every hop's signature + lifecycle + attenuation. Cycle-safe,
  depth-bounded, returns chain root-first.
- **`aggregate_verdict_chain`** MCP tool — applies precedence across all
  matched clauses from all Charters. Strictest wins. Each
  `matched_clauses` entry carries `source_charter_id`.
- **Two-hop demo** — `profiles/acme_corp.yaml` +
  `profiles/acme_assistant.yaml` + `scripts/demo_chain.py`. Defining
  case: a "Customer PII export" task is caught ONLY by the child
  Charter, proving the chain enforces the union of restrictions.
- **OpenAI Agents SDK adapter** — `charter.adapters.openai_agents` with
  `charter_preflight(charter_url, task)` + `@charter_gated(charter_url)`
  decorator. No hard dependency on `openai-agents`; install with
  `pip install -e '.[openai_agents]'`. Grader injection lets users keep
  all LLM traffic on a single provider.
- **fly.io deploy workflow** — `.github/workflows/deploy.yml` deploys on
  push to `main`, smoke-checks `/healthz`. Gated on
  `vars.DEPLOY_ENABLED == 'true'`.
- **158 tests** (was 103). 51 new.

---

## 8. Roadmap

The detailed iteration plan for v0.5 / v0.6 / v0.7 still lives in
[`ROADMAP.md`](ROADMAP.md). What follows is the forward-looking part.

### 8.1 v0.8 — Trust Model Upgrade (planning)

[Planning issue #17](https://github.com/hnaymyh123-henry/charter/issues/17)
· [Milestone v0.8](https://github.com/hnaymyh123-henry/charter/milestone/4)

Replaces v0's TOFU-on-first-fetch trust model. Three pieces that compose:

1. **JWKS endpoint** — issuers publish public keys at
   `/.well-known/jwks.json`. Calling agents fetch once per issuer and
   pin by `kid`. Charters gain `provenance.issuer_kid`.
2. **Key-fingerprint pinning** — `data/pins.json` records
   `principal_id → key_fingerprint` on first fetch; subsequent fetches
   verify the inline key matches the pin. Mismatch → new
   `CharterPinMismatchError`. Manual override via `charter pins reset
   <principal>`.
3. **Transparency log (append-only)** — every signed Charter is appended
   to `data/transparency.log` with a SHA-256 chain.
   `charter audit verify` walks the log; `GET /transparency/log` and
   `GET /transparency/proof/<charter_id>` expose it for third-party
   audit. `Charter.provenance.transparency_log_id` carries the entry
   offset.

Plus low-risk fixes bundled in: bump `pyproject.toml` version from
`0.1.0` to `0.7.0`, add a `charter audit` CLI namespace.

**Status:** scope sketched in issue #17, awaiting ✅/❌ on each work
item before issues are opened for the individual commits. The milestone
is empty so nothing is committed yet.

### 8.2 Beyond v0.8 (deferred backlog)

Tracked here so they don't get forgotten, but not on the near roadmap.

- **Semantic subset checking for Charter Chain.** v0.7 ships
  string-based. Semantic subset uses an LLM to decide whether child
  clauses are stricter restatements of parent clauses; v0.8+ work.
- **Privacy: Selective Disclosure JWT (SD-JWT)** for private clauses;
  zero-knowledge proofs for "Charter satisfies condition X" without
  revealing X.
- **More framework adapters.** v0.7 ships OpenAI Agents. LangGraph and
  CrewAI on the backlog.
- **Integrations.** Mem0/Letta auto-resync (so principal context
  changes re-project the Charter); AP2 payment terms; Web Bot Auth
  signed-header carrying `charter_url`.
- **Enterprise.** Connect to HRIS; auto-generate per-role Charters;
  audit interface for "did this agent violate its Charter in the last
  30 days?"
- **Capability-Boundary Enforcement.** Bind Charter checks to real
  resource gateways (DB, payments, filesystem, tool runtimes) so
  malicious calling agents cannot bypass the gate. Major scope; the
  natural v1+ story.
- **Charter marketplace / templates.** Browseable per-role profile
  templates ("standard accountant agent Charter").

---

## 9. References

- [`README.md`](README.md) — install, configure, run.
- [`CONTEXT.md`](CONTEXT.md) — canonical glossary for every term used here.
- [`ROADMAP.md`](ROADMAP.md) — v0.5 / v0.6 / v0.7 iteration plans (the
  detailed work-item breakdowns).
- [`AGENTS.md`](AGENTS.md) — what worker agents are expected to do under
  the protocol (the 5-step Compatibility Check loop).
- [`docs/legacy/hackathon-design.md`](docs/legacy/hackathon-design.md) —
  the original combined hackathon-era document. Preserved for historical
  reference; this PRODUCT.md supersedes it for current state.
- [GitHub releases](https://github.com/hnaymyh123-henry/charter/releases) — `v0.5.0`, `v0.6.0`, `v0.7.0` tags with detailed notes.
- [Open milestones](https://github.com/hnaymyh123-henry/charter/milestones?state=open) — `v0.8` (planning).
