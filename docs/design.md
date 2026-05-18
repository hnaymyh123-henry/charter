# Charter Design Rationale

*What this doc is: the "why" companion to the protocol specification — the structural arguments, tradeoffs, and explicitly deferred decisions behind Charter's v0 shape. For the normative contract (schema, decision rules, tool signatures), see [`spec.md`](./spec.md). For terminology, see [`../CONTEXT.md`](../CONTEXT.md). For the implementation milestones that follow from these decisions, see [`../ROADMAP.md`](../ROADMAP.md).*

---

## 1. Why a Principal / Authority Layer Is the Missing Piece

Existing agent protocols are all forward declarations of one axis of the agent's surface:

| Existing concept | Question it answers | Who declares | Grain |
|---|---|---|---|
| **Agent Card** (A2A) | What can I do? | Agent operator / framework | Per skill |
| **Identity** (Web Bot Auth) | Who am I? | Agent operator / CA | Per message |
| **Resume / Reputation** | What have I done? | Third parties | Aggregate |
| **Mandate** (AP2) | What did the user authorize for this task? | End user | Per transaction |

Stacked, these describe capability + identity + history + per-task authorization. They do not describe what the agent is **allowed or willing to do continuously, in the principal context it currently belongs to**.

Cloud-side IAM solved this in three layers a decade ago:

| Layer | Concept | State of the agent ecosystem |
|---|---|---|
| **Capability** | Can this thing technically do X? | Covered (Agent Card) |
| **Authority / Principal** | Does this thing act for someone, and is it allowed to do X? | **Empty** |
| **Authorization** | Is this specific request approved? | Covered (AP2 Mandate) |

Charter fills the middle layer. It is a structural gap, not a UX wish.

---

## 2. Why `principal x agent` Is the Binding Grain

The binding question is "what does this Charter actually attach to?" Charter attaches to the relationship pair, not to any of the obvious alternatives.

| Candidate grain | Why rejected |
|---|---|
| **Model** | The same model serves countless agents and principals; permission semantics at this grain are useless. |
| **Agent class / Agent Card** | Describes only what the agent technically can do — cannot express "for whom". An Agent Card is intrinsic to an agent; Charter is relational. |
| **Single task** | This is the grain of per-task authorization (AP2 Mandate). Charter is meant to outlive any single task. |
| **`principal x agent`** | Expresses the same agent serving different principals under different scope, refusals, hours, budgets, style, and escalation. This is the right grain. |

This grain produces a critical property: the same underlying agent can simultaneously hold many Charters — `alice@acme.com x research_agent_v1`, `bob@startup.io x research_agent_v1`, `BookkeeperBot x ocr_agent_v1` — and each is an independent Charter Instance. The calling agent always evaluates the Charter for the specific relationship it is about to invoke.

This is also why Charter cannot be folded into Agent Card:

| Argument | Detail |
|---|---|
| **Orthogonal concepts** | Agent Card describes intrinsic attributes; Charter describes relational attributes. Different objects. |
| **Different data owners** | Agent Card belongs to the agent operator or framework. Charter belongs to the principal or Charter Issuer. |
| **Different lifecycles** | Agent Card is stable. Charter changes with principal context (new project, temporary leave, role change). |
| **One-to-many** | One agent with N principals has N Charters. No single-Agent-Card field can carry this without becoming an array indexed by principal — which is what Charter already is. |

---

## 3. Why Versioned Natural-Language Clauses, Not Fixed Policy Fields

The temptation, given any prior IAM background, is to define a fixed taxonomy: `actions`, `resources`, `data_classes`, `conditions`, etc. v0 rejects this for a reason.

The agent task space is open. Today the work is tax filing. Tomorrow it is cleaning a database. Next week it is helping another agent redesign a workflow. A pre-enumerated taxonomy either:

1. Bloats into an enterprise policy table that nobody actually fills in, or
2. Locks out task types nobody anticipated at schema time.

v0 chooses **versioned natural-language clauses + a structured verdict contract + a protocol-constant type mapping** instead. The result has three properties:

- Each clause has a stable `id` and a `type` drawn from a small closed set.
- The clause `text` is natural language, which keeps the policy surface as expressive as the underlying language model.
- `TYPE_TO_DECISION` fixes the mechanical contribution of each type, so the LLM never freely decides `allow` / `needs_approval` / `incompatible` — it only judges hits.

Common high-frequency clauses can later be compiled into machine fields without being part of the v0 protocol surface. The design is forward-compatible with that compilation, not foreclosed by it.

---

## 4. Why LLM-First, Schema-Bound Checks

The Compatibility Check is intentionally split:

- **LLM-first** because the input ("does this open-ended task hit this clause?") is exactly the kind of fuzzy semantic judgment language models do well and rule systems do badly.
- **Schema-bound** because the output must be deterministic, auditable, and composable. The LLM never returns a free-form verdict; it returns per-clause hits with confidence, and a protocol-side aggregator produces the verdict.

Two consequences fall out of this split:

1. **The calling agent's own LLM is the judge.** There is no central judging API. Operators don't need to trust an external service to decide what their agents may do — they only need to trust the deterministic aggregator and the signed Charter content. This also matches where the rest of the calling agent's reasoning already lives.

2. **The aggregator is unit-testable.** `incompatible > needs_approval > allow` is three lines of code. The interesting logic moves into clause text (which is signed and reviewable) and into the LLM prompt (which is observable).

This separation also makes it cheap to swap in stronger judges later — for example a fine-tuned classifier — without changing the protocol surface.

---

## 5. Why Self-Attesting Charter + HTTPS as the v0 Trust Root

v0 ships the thinnest possible trust model that still resists casual tampering:

```
TLS / HTTPS               trusts the hosting domain (CA system)
        |
Charter JSON              contains issuer_public_key + issuer_signature
        |
Calling agent             verifies issuer_signature with the embedded key
```

The Charter carries its own public key inline. A calling agent fetches one URL, verifies the signature, and is done — no JWKS round-trip, no DID resolution, no certificate chain validation, no external PKI lookup. This was a deliberate choice over several heavier alternatives:

| Alternative | Why deferred |
|---|---|
| JWKS endpoint (`/.well-known/jwks.json`) | One more fetch, one more endpoint to host, and demos and integration tests have to explain it. |
| DIDs (decentralized identifiers) | Heavy infrastructure overhead for a protocol that does not yet need decentralized identity. |
| X.509 certificate chain | Disproportionate engineering effort for the threat surface v0 actually faces. |
| `service_attestation` second-layer signature | Adds complexity without protecting against the threats that matter at v0 (issuer-key compromise is the real risk, and a second layer does not help with that). |

### What this trust model does not protect against

The v0 model is honest about its limits. These are documented gaps, not bugs:

- **TOFU on first fetch.** The first time a calling agent sees a `charter_url`, it has no way to independently verify that the embedded `issuer_public_key` belongs to the stated principal. Trust collapses onto HTTPS and the hosting domain.
- **Weak key-rotation discovery.** If an issuer rotates keys, the new Charter just carries the new key. Old callers have no fingerprint-pinning mechanism to detect that the key changed.
- **No defense against host compromise.** If the host of a Charter is compromised, an attacker can sign and serve arbitrary Charters.

These are addressed by deferred items: **JWKS endpoint with key-fingerprint pinning** (lets callers detect unexpected rotation), **a transparency log** in the Certificate-Transparency style (lets the principal audit which Charters have been issued in their name), and **`service_attestation` as a second signature** (a hosted service attesting that the issuer signature was produced through its own audited flow). All three live on the deferred backlog, not the current critical path.

---

## 6. Why Charter Is a Voluntary Protocol

Charter is a **Delegation Gate**, not a **Capability-Boundary Enforcement** mechanism. The distinction is precise:

| Layer | What it actually blocks | Who has to cooperate |
|---|---|---|
| **Delegation Gate** (v0) | A compliant calling agent's decision to send a task | The calling agent |
| **Capability-Boundary Enforcement** (deferred) | The actual resource operation (DB write, payment send, file delete) | The resource gateway |

v0 is voluntary on purpose. The argument for shipping a voluntary protocol first:

1. **Precedent.** `robots.txt` has been voluntary for 30 years and runs the indexable web. Cloudflare Web Bot Auth is also voluntary. Voluntary protocols work in reputation-sensitive ecosystems.
2. **Iteration speed.** Resource-level enforcement requires integration with every resource gateway (DB, payments, filesystem, tool runtimes). That is a multi-quarter project. The Delegation Gate is a few hundred lines of code.
3. **Compatibility surface.** A voluntary protocol can be adopted incrementally. A capability-boundary system has to be in front of every operation or it adds nothing.

The honest claim Charter v0 makes: "for calling agents that follow the protocol, here is a stable, low-cost, auditable way to decide whether to delegate." Capability-boundary enforcement — Charter checks bolted onto DB gateways, payment APIs, tool runtimes — is on the deferred backlog as the v1+ story.

---

## 7. Why `propose_within_scope` Is Part of the Protocol

A naive Charter that only returned `allow / needs_approval / incompatible` would behave like a compliance refusal system. Useful, but inert: calling agents would either give up or hammer the gate.

`propose_within_scope` changes the protocol from a refusal list into a **delegation router**. When the verdict is `incompatible`, the calling agent can ask "given this Charter, what task could I legitimately delegate to this agent instead?" — and get back a Charter-grounded rewrite with referenced clauses.

Two consequences fall out of this:

1. **It elevates the protocol's status from gate to coordinator.** Refusal alone tells the caller "not here." Rewrite plus refusal tells the caller "not this, but here." That is the difference between a permissions system and a marketplace-of-agents protocol.
2. **It puts pressure on the clause design.** Clauses that produce useful rewrites tend to be specific (`accounting / tax / bookkeeping`) rather than vague (`work-related tasks`). The rewrite path is therefore a forcing function for clause quality.

v0 implements `propose_within_scope` as single-shot with no loopback. Full **Loopback Verification** — feed the rewrite back through Compatibility Check, anneal the LLM temperature, retry on failure, return a structured `RewriteFailure` on exhaustion — is documented and deferred to v0.6. The single-shot version is enough to demonstrate that the protocol is a router, not a refuser; the loopback version is what makes the router production-grade.

---

## 8. Why Charter Chain Attenuation Is Deferred

A two-hop Charter Chain (the agent-as-principal case) is the natural next demo: Alice issues a Charter to BookkeeperBot, which in turn issues an attenuated Charter to an OCR sub-agent it employs. Each downstream Charter must be a subset of the upstream one.

This is on the near roadmap (v0.7 in [`../ROADMAP.md`](../ROADMAP.md)) but explicitly out of scope for v0. The reasons:

1. **The single-Charter check is the load-bearing primitive.** Chains compose single-Charter checks; you cannot meaningfully demo chains until the single-Charter case is solid. Solving them in the wrong order produces a system that looks impressive in slides and fails on basics.
2. **Attenuation semantics need clause-level support that v0 does not have.** A clean chain check requires either string-match subset rules on clause text (crude) or semantic subset reasoning (much harder). v0 ships the underlying clause structure; v0.7 adds the chain logic on top.
3. **The most common single-actor case is a human principal x one agent.** Charter Chain is the multi-agent case. v0's whole purpose is to make the simple case land first.

The schema is already chain-ready (`principal_chain[]` is reserved and `lifecycle.replaces` / `replaced_by` already model supersession). The runtime machinery is what's deferred.

---

## 9. Anti-Goals: What Charter Is Not

Charter is repeatedly mistaken for things it deliberately is not. For clarity:

### Not an Agent Card extension

Agent Card describes an agent in isolation. Charter describes a relationship. Different objects, different data owners, different lifecycles, different cardinality. Adding a `charter_url` reference to an Agent Card is fine and encouraged; merging the two artifacts is not.

### Not a replacement for Constitutional AI or alignment training

Constitutional AI shapes an underlying model's behavior at training time. Charter is a runtime, external, queryable declarative layer about a specific principal-agent relationship. They are complementary: Constitutional AI says what a model will and won't do regardless of context; Charter says what this specific deployment of that model, acting for this specific principal, is supposed to do.

### Not an enterprise IAM product

Enterprise IAM (Okta, Microsoft Entra, Google Cloud Agent Identity) targets corporate IT administrators with role-based, top-down policy. Their data sources are HRIS and AD. Their product surface is admin consoles. Charter targets individual principals (or small organizations acting on behalf of one), uses the principal's own context as the data source, projects clauses via LLM rather than admin entry, and has no console-based deployment story. The markets do not overlap; the design choices that fit one are wrong for the other.

### Not a guarantee against malicious agents

A malicious calling agent can ignore the protocol entirely. v0 acknowledges this and scopes its claims accordingly — it improves the behavior of cooperating agents, full stop. Hard enforcement is the Capability-Boundary Enforcement work on the deferred backlog.

### Not a public dump of principal data

The Public Charter is a work contract, not a memory dump. Everything in `provenance.source_commitments` is a commitment (type + summary + hash). The original Profile YAML, memory, conversation history, and source documents stay private. A reader of the Charter learns enough to decide whether to delegate; they do not learn how Alice phrased her preferences.

---

## Footer

The original combined hackathon-era source for this design is preserved at [`./legacy/hackathon-design.md`](./legacy/hackathon-design.md) for historical reference. That document interleaves demo scripts, pitch material, and competitive framing with the structural arguments above; this file is the rationale slice extracted from it.
