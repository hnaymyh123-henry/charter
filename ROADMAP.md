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

## v0.8 — Trust Model Upgrade (SHIPPED 2026-05-19)

Replaces v0's TOFU-on-first-fetch trust model with a layered model.

1. **JWKS endpoint** (`/.well-known/jwks.json`) + `provenance.issuer_kid`.
2. **Key-fingerprint pinning** at `data/pins.json` + `charter pins` CLI.
3. **SHA-256-chained transparency log** + `GET /transparency/{head,log,proof}`
   + `provenance.transparency_log_id` + `charter audit verify/show` CLI.
4. CHANGELOG.md, `pyproject.toml` 0.1.0 → 0.8.0, `[tool.bumpversion]`.

Released as PRs #19-#25. See `CHANGELOG.md` for the per-PR detail.

---

## v0.9 — Production-readiness + Ecosystem (Batch 1 SHIPPED 2026-05-23)

Batch 1 closes the "fully production-grade protocol" gap. 6 PRs landed
together:

- **A1 Chain semantic subset** (LLM-based `verify_chain_semantic` with
  mode-aware `verify_chain(mode="strict"|"semantic"|"auto")` + cache by
  `(parent_charter_id, parent.issued_at)` + MCP tool #11 + `CharterChainGraderError`).
  PR #34 / Issue #26.
- **A5 AP2 Mandate integration** (`charter.adapters.ap2` + `AP2VerifyResult`
  schema + end-to-end demo). PR #32 / Issue #27.
- **A6 Web Bot Auth signed-header adapter** (self-implemented RFC 9421
  Ed25519 subset + `sign_request` / `verify_request` / `gated_middleware`
  + Cloudflare-style edge enforcement). PR #33 / Issue #28.
- **Priv-1 Redaction + SD-JWT selective disclosure** (`Clause.private_fields`,
  `charter/privacy.py`, `data/disclosures/<charter>/<id>.json`,
  bearer-token `GET /disclosures/{charter_id}/{disclosure_id}`). PR #35 / Issue #31.
- **B1.4 Adversarial test suite** (5 attack categories, 28 cases, 2 xfail
  linked to follow-up; `FakeAnthropicClient` fixture; `docs/threat-model.md`;
  CI step with `continue-on-error`). PR #36 / Issue #29.
- **B3.9 Cookbook** (10 scenario guides under `docs/cookbook/` + 13 runnable
  examples under `examples/cookbook/`). PR #37 / Issue #30.

Cumulative: +11.6k lines, +104 new tests (259 → 363+), tagged ADR-003
disclosure exception + ADR-010 string fallback + ADR-011 path-1 SHIPPED.

### Batch 2 — SHIPPED 2026-05-24 (6 PRs + 1 hotfix)

- **A8 Postgres reference adapter** — capability-boundary proof,
  `charter/adapters/postgres/` ~700 LOC, fail-closed PG wire proxy,
  PRODUCT.md §5.6 + ADR-006 evolution wording (PR #48 / Issue #41).
- **B1.1 Conformance test suite** — language-neutral 44 JSON vectors,
  SPEC.md, Python runner (complete), JS / Rust skeleton runners,
  CI gate (PR #47 / Issue #42).
- **B1.3 Revocation propagation** — Cache-Control middleware +
  `GET /transparency/revoked` NDJSON + `RevocationAwareCache` SDK
  helper (PR #45 / Issue #38).
- **B2.7 OTel semconv** — `charter/observability.py` no-op fallback +
  6 spans (private `charter.*` namespace) + `docs/observability.md`
  (PR #46 / Issue #40). Hotfix `5ebc6f3` for `unused-ignore` CI
  regression follows.
- **B3.8 Charter Inspector Web UI** — `/inspect` route, HTMX/Alpine.js,
  URL allowlist + SSRF guard, QA verified 32/32 attack PoC (PR #44 /
  Issue #39).
- **B3.10 Performance baseline** — `benchmarks/` (5 files), pytest-
  benchmark, `docs/performance.md`, CI opt-in `[bench]` gate (PR #49 /
  Issue #43).

Cumulative across Batch 2: +133 tests (363 → 474+), 439 collected
post-merge.

### Batch 3 — PRs open 2026-05-26 (2 features + 1 chore)

- **B2.5 Negotiation / step-up protocol** — PR #53 / Issue #51.
  `AdHocGrant` schema + Ed25519 signing + `data/grants/<id>.json`
  storage with path-traversal defense + `POST /step-up` (3 approval
  modes: `auto-deny` / `auto-approve` / `callback`) + rate-limit
  `(principal_id, agent_id) ≤ 5 / 60s` + `GET /grants/{id}` (404 / 410
  surface) + MCP tool 12 `request_step_up` (HTTP-forward only) + AP2
  mandate `extensions.ad_hoc_grant_id` extension that promotes
  Charter verdict `incompatible`/`needs_approval` → `allow` when a
  verified grant covers the task. ~1759 LOC + 56 new tests. Grants
  are **not** transparency-logged in v0.9 (ADR-013 future-work).
- **B1.2 JavaScript / TypeScript SDK** — PR #54 / Issue #50.
  `@charter/core` (`js/` subtree). Verification-only TS port:
  schema (zod), canonical bytes, Ed25519 sign/verify (`@noble/ed25519`
  v2), aggregate, strict chain, lifecycle, JWK + kid, pin fingerprint,
  transparency log, SD-JWT path 1 privacy. 12 source files / 1352 LOC
  + 11 vitest files / 110 cases (all green); 4 cross-language vectors
  from `conformance/vectors/sign/` anchor byte equivalence with the
  Python reference. `tsup` dual-format (ESM + CJS + `.d.ts`).
- **chore: env-fix follow-ups** — PR #55. Tightens
  `pytest.importorskip("opentelemetry")` → `opentelemetry.sdk` (the
  api package is often transitively installed, the SDK is not);
  adds `pytest.importorskip("sqlglot")` to the postgres adapter tests
  so a `[dev]`-only env skips them cleanly instead of erroring at
  collection; and pays down the 3 pre-existing `inspector.py`
  `no-any-return` mypy errors. Net: full suite goes from 6 failures
  + 10 errors + 2 collection errors → **427 passed, 12 skipped,
  2 xfailed**; `mypy charter/` is clean (32 files).
- Optional / not picked up: ADR-011 path 2 (delegated grading
  endpoint) — pulled forward from "Beyond v0.9 deferred" if the
  privacy stack should grow next. Punted to keep Batch 3 focused.

**Phase 4 status (2026-05-26):** all 3 PRs opened, awaiting code
review. Local pre-flight:
- B2.5: 56/56 new tests pass, ruff + mypy clean on diff.
- B1.2: 110/110 vitest cases pass, `tsc --noEmit` clean, `tsup` build
  succeeds.
- chore: full pytest suite green in `[dev]` env, `mypy charter/`
  clean, `ruff check` clean.

---

## Beyond v0.9 (deferred backlog)

- **Privacy paths 2/3.** Path 2 (delegated grading endpoint where the
  issuer runs grading and returns only the verdict) requires an optional
  "issuer-as-trusted-grading-oracle" protocol mode. Path 3 (ZK proofs of
  "Charter satisfies condition X") waits for the ZK + LLM tooling stack
  to mature.
- **Mem0 / Letta auto-resync.** Deferred — opens a memory-poisoning →
  privilege-escalation surface and breaks caller cache freshness.
- **HRIS integrations.** Anti-goal per PRODUCT.md §6; Charter stays a
  neutral protocol, integrators write their own connectors.
- **Full Capability-Boundary Enforcement.** A8 ships a Postgres reference
  adapter as a pattern proof; binding to Stripe / S3 / arbitrary tool
  runtimes is multi-quarter work and remains v1+.
- **Charter marketplace / templates.** Browseable per-role profile
  templates ("standard accountant agent Charter") — gate on real adoption.
- **More framework adapters** beyond OpenAI Agents (SHIPPED v0.7) and
  Anthropic SDK (deferred / low priority per ADR-012). LangGraph + CrewAI
  explicitly off-roadmap.

---

## Tracking

Each iteration becomes its own milestone. Each work item becomes its own
issue. PRs reference the issue number in the title.

`v0.5` → `v0.6` → `v0.7` → `v0.8` → `v0.9` corresponds roughly to:
*hygiene → completion → extension → trust → production-readiness*. The
first one was the boring one — boring was the point.
