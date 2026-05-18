# Charter Roadmap

From hackathon v0 prototype to landed open-source project. Three focused
iterations, ~1 week each part-time. Each builds on the previous, ordered
so the lowest-risk, highest-leverage work happens first.

> **Context.** For *what Charter is and how it works*, see
> [`PRODUCT.md`](PRODUCT.md). This file is the prioritized iteration
> plan — what we built, in what order, and what's deferred. The two
> documents are kept in sync but serve different purposes.

> **What "done" looks like.** v0.7 ships a deployed `https://charter.<host>/`
> with two-hop Charter Chain attenuation, one framework adapter, signed
> typed exceptions, propose_within_scope with loopback verification, real
> CI, and an Apache-licensed package on PyPI.

---

## v0.5 — Project Hygiene

**Goal:** turn a working prototype into something a stranger can clone,
trust, contribute to, and run in CI without breakage.

### Work items

1. **Typed exceptions module** — `charter/errors.py` with `CharterError`,
   `CharterNotFoundError`, `CharterSignatureError`, `CharterExpiredError`,
   `CharterRevokedError`, `CharterSchemaError`. Refactor
   `mcp_server._fetch_and_verify` and `signing.verify_charter` callers to
   raise them instead of `ValueError("CharterNotFoundError: ...")`. ~80 LOC.
   **Done when:** `pytest tests/test_errors.py` asserts each path raises the
   correct typed exception.

2. **CI workflow** — `.github/workflows/ci.yml` matrix on Python 3.12/3.13,
   Ubuntu + macOS + Windows. Jobs: install via `uv`, lint, type-check,
   pytest. Badge in README. ~60 LOC, 1 file.

3. **Linting + formatting + type checking** — add `ruff` (lint + format) and
   `mypy --strict` for `charter/`. New `[tool.ruff]` and `[tool.mypy]`
   sections in `pyproject.toml`. Fix the backlog that surfaces (mostly
   missing annotations on `_safe`, `_canonical_bytes`, `Any`-returning
   helpers in `mcp_server.py`). Wired into CI.

4. **FastAPI server tests (pytest-asyncio)** — new `tests/test_server.py`
   covering `GET /{p}/{a}` 200, 404, signature-mismatch, `/api/lookup`
   happy + 404, root index, expired-but-served path. ~120 LOC. Server
   coverage ≥ 90%.

5. **`.well-known/charter/{agent_id}` self-hosted mode** — schema and term
   already exist; trivial second route in `server.py` gated on a
   `CHARTER_SELF_HOSTED_PRINCIPAL` env var. ~25 LOC. Documented in README.

6. **Container + uvicorn production config** — `Dockerfile` (multi-stage
   `python:3.12-slim`), `.dockerignore`, `GET /healthz`, `fly.toml`
   template. ~70 LOC across 3-4 files. `docker build && docker run`
   exposes Charter on :8000.

7. **Documentation reorganization** —
    - Split `Charter-黑客松项目文档.md` into `docs/spec.md` (protocol
      contract: schema, `TYPE_TO_DECISION`, aggregation rule, MCP tool
      signatures) + `docs/design.md` (rationale, ADR-style "why",
      deferred decisions).
    - Move `CONTEXT.md` out of `.gitignore` — it's a clean public
      glossary, not internal design notes.
    - Keep the old hackathon doc as `docs/legacy/hackathon-design.md`
      with a header note.
    - README links to `docs/spec.md` as the authoritative protocol
      reference.

**Out of scope for v0.5:** anything that changes the protocol or MCP tool
surface; revoke/renew commands; `propose_within_scope`.

---

## v0.6 — Protocol Completion

**Goal:** ship the three v0-designed-but-unbuilt protocol features so the
implemented protocol surface matches the spec.

### Work items

1. **`propose_within_scope` MCP tool (single-shot)** — add to
   `charter/mcp_server.py`. Inputs `(charter_url, intended_task,
   failed_verdict_dict)` → calls Anthropic via dependency-injectable
   client → returns `RewriteProposal` schema. Gracefully degrades to
   `rewrite_available: false` if no API key. Keeps `aggregate_verdict`
   LLM-free. ~120 LOC. Integration test with stubbed Anthropic via
   `respx`.

2. **Loopback verification with retry + temperature anneal** — wrap the
   single-shot above with up to N=3 attempts. Each attempt feeds the
   rewrite back through per-clause hit-grading + `aggregate_verdict`,
   anneals temperature 0.2 → 0.7, evolves the prompt with failing clause
   IDs. Returns `RewriteProposal` on success or
   `RewriteFailure(attempts=[...])` on exhaustion. New
   `charter/loopback.py`, schema additions. ~150 LOC.

3. **`charter revoke` CLI** — loads the Charter, sets
   `status="revoked"`, `revoked_at=now`, re-signs (the revocation itself
   must be signed), saves. After this, `_fetch_and_verify` raises
   `CharterRevokedError`. ~40 LOC + 1 test.

4. **`charter renew` CLI** — loads existing Charter, builds new one with
   same clauses + new `charter_id` + fresh `issued_at`/`valid_until`,
   sets `replaces=<old_id>` on new and
   `replaced_by=<new_id>` + `status="superseded"` on old, re-signs both.
   ~60 LOC + round-trip test.

5. **Charter Discovery directory file + SDK helper** —
   `resolve_charter_url(principal, agent)` is referenced in CONTEXT.md
   but doesn't exist. Write `data/charters/index.json` on every
   `save_charter`; new `charter/discovery.py` with the helper; matching
   `/api/lookup` server route (already partly there). ~70 LOC.

6. **Structured logging** — `structlog` (or stdlib + JSON formatter). Log
   every `_fetch_and_verify` outcome, every CLI lifecycle command, every
   MCP tool entry. `CHARTER_LOG_FORMAT=json` produces parseable lines.
   ~50 LOC.

7. **Encrypted private keys at rest** — `save_private_key` currently
   writes plaintext PEM. The single biggest "do not ship" footgun. Use
   `cryptography.serialization.BestAvailableEncryption` with a passphrase
   from `CHARTER_KEY_PASSPHRASE`; fall back to plaintext + loud WARN log
   if unset (preserves dev UX). ~30 LOC.

**Out of scope for v0.6:** Charter Chain, framework SDKs, JWKS,
transparency log, AP2, Web Bot Auth.

---

## v0.7 — Charter Chain Demo

**Goal:** ship the smallest visible extension that proves the protocol
generalizes — multi-hop scope attenuation — because it's the only
deferred item whose runtime behavior is dramatically different from v0.6.

**Why this and not JWKS or full framework matrix:** JWKS replaces TOFU but
the demo story doesn't change. Framework adapters are high-LOC integration
work with low demo signal per LOC. Charter Chain is the agent-as-principal
case, attenuation, recursive verify — visibly distinct, reuses everything
from v0.5/v0.6.

### Work items

1. **Chain schema + attenuation check** — extend `Charter` with optional
   `parent_charter_url` and `attenuation_proof`. New `charter/chain.py`
   with `verify_chain(charter, parent) -> bool` that checks downstream
   clauses are a subset of / stricter than upstream (string-match on
   `out_of_scope` clauses is fine for v0.7; semantic subset is v0.8).
   ~120 LOC.

2. **`fetch_charter_chain` MCP tool** — walks `parent_charter_url`,
   verifies each hop's signature, runs `verify_chain` at each step,
   returns the chain root-first. ~60 LOC. MCP integration test with two
   stubbed Charter URLs.

3. **`aggregate_verdict_chain`** — accepts `(chain,
   hits_per_charter)` and applies precedence across all hops. Any
   `incompatible` anywhere in the chain → final `incompatible`, applied
   clause sourced from whichever charter forced it. ~40 LOC.

4. **Two-hop demo profile + script** — `profiles/acme_corp.yaml` (org
   principal) issues a Charter to `acme_assistant_agent`.
   `profiles/acme_assistant.yaml` (agent-as-principal) issues a Charter
   to `research_agent_v1` referencing the corp Charter as parent. New
   `scripts/demo_chain.py` runs the end-to-end in < 30s.

5. **One framework adapter (OpenAI Agents SDK)** — pick the smallest of
   the three. New `charter/adapters/openai_agents.py` with a
   `charter_preflight(charter_url, task)` function, documented as a
   `pre_run` hook. ~80 LOC + 1 example in `examples/`.

6. **Public deployment** — `fly.toml` already in v0.5; now deploy. GitHub
   Action on `main` push, secret config docs in README. Goal:
   `https://charter.<your-host>/alice@acme.com/research_agent_v1`
   resolves publicly.

**Out of scope for v0.7:** JWKS, public-key pinning, transparency log,
selective disclosure, Mem0/Letta auto-resync, AP2 integration, Web Bot
Auth, semantic subset-checking for chains, full framework matrix
(LangGraph, CrewAI), enterprise/HRIS integration.

---

## Beyond v0.7 (deferred backlog)

Tracked here so they don't get forgotten, but not on the near roadmap:

- **Trust model upgrade.** JWKS endpoint, key-fingerprint pinning,
  transparency log (Certificate-Transparency-style), `service_attestation`
  second layer.
- **Privacy.** Selective Disclosure JWT (SD-JWT) for private clauses;
  zero-knowledge proofs for "Charter satisfies condition X" without
  revealing X.
- **Integrations.** Mem0/Letta auto-resync; AP2 payment terms reference;
  Web Bot Auth signed-header carrying `charter_url`; LangGraph + CrewAI
  adapters.
- **Enterprise.** Connect to HRIS, generate per-role Charters
  automatically; audit interface for "did this agent violate its
  Charter in the last 30 days?"
- **Capability-Boundary Enforcement.** Bind Charter check to real
  resource gateways (DB, payments, filesystem, tool runtimes) so a
  malicious calling agent cannot bypass the gate.
- **Charter marketplace / templates.** Browseable per-role profile
  templates ("standard accountant agent Charter").

---

## Tracking

Each iteration becomes its own milestone. Each work item becomes its own
issue. PRs reference the issue number in the title.

`v0.5` → `v0.6` → `v0.7` corresponds roughly to:
*hygiene → completion → extension*. The first one is the boring one.
Boring is the point.
