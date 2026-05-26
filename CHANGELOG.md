# Changelog

All notable changes to Charter are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Step-up protocol — AdHocGrant + `request_step_up` (B2.5)** — PR #53.
  The dual of `propose_within_scope`. Where the rewrite path adjusts the
  *task* to fit the Charter, an `AdHocGrant` temporarily authorises the
  *task* outside the Charter's normal scope. A grant is a **sibling of
  Charter, not a child** — no Charter field is mutated. Signed with the
  same Ed25519 issuer key (ADR-002), canonical-bytes rule mirrors
  Charter's (`issuer_signature` cleared, ADR-003), and has no `revoked`
  lifecycle state — short TTL (60s ≤ delta ≤ 3600s) is the only safety
  primitive (ADR-013). New `charter.grants` module persists grants under
  `data/grants/<grant_id>.json`. `POST /step-up` runs rate-limit
  `(principal_id, agent_id) ≤ 5 / 60s` then dispatches per
  `CHARTER_STEPUP_APPROVAL_MODE` (`auto-deny` default / `auto-approve` /
  `callback`); `GET /grants/{id}` returns 200 / 404 / 410. MCP tool
  `request_step_up` is HTTP-forward only — ADR-009 keeps the decision
  policy server-side. AP2 adapter learns `extensions.ad_hoc_grant_id`:
  a verified, unexpired grant whose `task` covers the mandate's task
  promotes a Charter verdict of `incompatible` / `needs_approval` up
  to `allow`; any failure leaves the Charter verdict untouched (no
  fail-open). New errors `CharterGrantNotFoundError` /
  `CharterGrantExpiredError` / `CharterGrantSignatureError`. Grants are
  NOT transparency-logged in v0.9 (ADR-013 future-work).

- **`@charter/core` SDK — JavaScript / TypeScript (B1.2)** — PR #54.
  Verification-only TS/JS port of the Python reference, shipping under
  `js/` (new subtree). Cross-language **byte-equivalent**: same canonical
  bytes, same SHA-256 digests, same kid as Python — anchored by 4 of the
  44 conformance vectors at `conformance/vectors/sign/`. 12 source files
  / 1352 LOC: `schema.ts` (zod, `.strict()` per model), `canonical.ts`,
  `signing.ts` (@noble/ed25519 v2, ADR-002), `aggregate.ts`, `chain.ts`
  (strict 4-rule attenuation; `auto` mode degrades to strict — no LLM
  client to thread), `lifecycle.ts` (4 classifications: revoked >
  superseded > expired > usable), `jwks.ts` (kid + RFC 7517 JWK),
  `pins.ts` (full sha256 fingerprint), `transparency.ts` (verifyLogChain
  over parsed entries), `privacy.ts` (redactClause + verifyDisclosure +
  matchRedacted), `constants.ts`. 11 vitest files / 110 cases — all
  green. `tsup` dual-format (ESM + CJS + `.d.ts`); `tsc --noEmit` clean
  under strict + noUnusedLocals + noUnusedParameters. Out of scope for
  `@charter/core` (deferred to a future `@charter/server`): HTTP server,
  MCP server, CLI, encrypted private keys at rest, JWKS HTTP fetch,
  pin store, disclosure HTTP fetch, semantic chain mode.

- **Charter Inspector Web UI (B3.8)** — `GET /inspect?url=<charter_url>` and
  `GET /inspect/{principal}/{agent}` render a fetched Charter as a
  human-readable HTML page with status badge, foldable clause list (Alpine.js),
  signature / pin / JWKS / lifecycle / transparency verify panel, chain tree
  for `parent_charter_url`, and unified-diff vs `lifecycle.replaces`. The
  inspector reuses `mcp_server._fetch_and_verify` for every URL it touches,
  so JWKS / pin / lifecycle / signature checks run on every hop (ADR-007).
  Pulls jinja2 + HTMX + Alpine.js from CDN; jinja2 ships in the new
  `[project.optional-dependencies] inspector` group (`pip install
  charter[inspector]`). Without the extra, `/inspect` returns a 503 with an
  install hint and the rest of the server stays unaffected. SSRF guard
  rejects `file://`, `ftp://`, RFC 1918, loopback, and link-local targets
  unless `CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS=1` (local-dev override).

### Fixed

- **Test-skip guards for optional extras** — PR #55. `test_observability.py`
  changed `pytest.importorskip("opentelemetry")` →
  `importorskip("opentelemetry.sdk")` so the skip triggers correctly when
  `opentelemetry-api` is transitively installed but the SDK is not.
  `tests/adapters/test_postgres_{intent,proxy}.py` gained module-level
  `pytest.importorskip("sqlglot")` so a `[dev]`-only env (without the
  `postgres_proxy` extra) skips them cleanly instead of erroring at
  collection. Net: full suite goes from 6 failed + 10 errored + 2
  collection errors → 427 passed / 12 skipped / 2 xfailed.

- **`charter/inspector.py` mypy `no-any-return`** — PR #55. Bind
  `jinja2.Template.render(...)` (typed as `Any`) to an explicit
  `rendered: str` local in the three `render_*` helpers so
  `mypy --strict` is clean on the full `charter/` tree (32 files).

## [0.8.0] — 2026-05-19 — Trust model upgrade

Charter graduates from the v0 self-attesting trust model (TOFU on first
fetch, inline `issuer_public_key` is the only root) to a layered trust
model with three independent checks any one of which is hard to defeat.

### Added

- **JWKS endpoint** (`GET /.well-known/jwks.json`) publishes the issuer's
  public keys as RFC 7517 JWKs. Multi-tenant mode lists every known
  `(principal, key)` pair with a non-standard `iss` extension; self-hosted
  mode (`CHARTER_SELF_HOSTED_PRINCIPAL=…`) filters to one principal.
- **`Provenance.issuer_kid`** — 16-hex-char SHA-256-truncated key id, part
  of the signed payload so a post-sign kid swap breaks verification.
- **JWKS client** (`charter/keys.py`) with 5-minute TTL cache
  (`CHARTER_JWKS_CACHE_TTL` override), typed `JWKSNotFoundError` /
  `JWKSParseError`. `_fetch_and_verify` now cross-checks the inline
  `issuer_public_key` against the JWKS-published key for the Charter's
  `kid`; mismatch → `CharterKeyMismatchError`.
- **Key-fingerprint pinning** (`charter/pins.py`) stored at
  `data/pins.json` (`CHARTER_PIN_FILE` override). First fetch records
  the fingerprint (`sha256:<64-hex>` of the raw key bytes); subsequent
  fetches require a match. Mismatch raises `CharterPinMismatchError` and
  the operator must run `charter pins reset <principal>` after a
  legitimate rotation. `charter pins list` pretty-prints the table.
- **Transparency log** (`charter/transparency.py`) — append-only
  `data/transparency.log` (`CHARTER_TRANSPARENCY_LOG` override) of every
  signed Charter, SHA-256 chained (`prev_hash` ← previous `entry_hash`,
  genesis is 64 zeros). Stores identifiers + signature + chain only —
  no clause / summary / role text. Atomic writes via temp + rename.
  `sign_charter` appends on each call; idempotent on `charter_id` so
  revoke / renew re-signs do not duplicate the issuance record.
- **Transparency HTTP endpoints** —
  `GET /transparency/head`,
  `GET /transparency/log[?since=N]` (`application/x-ndjson`),
  `GET /transparency/proof/{charter_id:path}` (entry + linear chain).
- **`Provenance.transparency_log_id`** — the Charter's `seq` in the log,
  populated by `sign_charter` after the append. Excluded from the
  signed canonical bytes (chicken-and-egg) — a post-sign edit does NOT
  invalidate `verify_charter`.
- **`charter audit` CLI** — `charter audit verify [--remote ORIGIN]
  [--since SEQ]` walks and verifies the chain (local file or remote
  `/transparency/log`); exit 0 ok / 1 chain broken / 2 fetch error.
  `charter audit show <charter_id>` pretty-prints an entry plus every
  other entry from the same principal.
- **`pyproject.toml` version bumped** from `0.1.0` (frozen since the
  hackathon prototype) to `0.8.0`. Added `[tool.bumpversion]` config so
  future bumps are one command (`bump-my-version bump minor`).
- **CHANGELOG.md** — this file, backfilled with v0.5 / v0.6 / v0.7.

### Changed

- `_fetch_and_verify` order of operations:
  signature → JWKS cross-check (if kid) → pin check → lifecycle.
- `_canonical_bytes` now clears both `issuer_signature` AND
  `transparency_log_id` before serializing for signing.

### Compatibility

- Charters issued before v0.8 (no `issuer_kid`, no `transparency_log_id`)
  keep verifying under the v0 self-attesting model.
- A v0.8 Charter that carries a `kid` MUST have its issuer publishing a
  JWKS — a v0.7 client with no JWKS support still verifies via the
  inline key, but a v0.8 client treats an unreachable JWKS as fatal
  for that Charter (strict trust).

## [0.7.0] — 2026-05-19 — Charter Chain + framework adapter + deploy

Multi-hop delegation: one Charter can attenuate another's scope, and an
auditable chain proves the relationship. Plus the first framework
adapter and a fly.io deploy pipeline.

### Added

- **Charter Chain schema** — `Charter.parent_charter_url`,
  `Charter.attenuation_proof`, and `MatchedClause.source_charter_id`.
- **`fetch_charter_chain(charter_url, max_depth=5)`** walks the parent
  chain to the root, verifying each hop's signature, lifecycle, AND
  attenuation relationship. Cycle detection + depth bound.
- **`aggregate_verdict_chain(chain, hits_per_charter)`** combines
  per-clause hits across a chain into a single Verdict using the same
  `incompatible > needs_approval > allow` precedence as single-Charter
  aggregation. Strictest Charter wins; matched_clauses carries
  `source_charter_id`.
- **String-based subset rules** for `verify_chain(child, parent)` — the
  v0.7 attenuation check (semantic subset deferred to v0.8+).
- **Two-hop demo** under `demo/`: `acme_corp` → `assistant` → `research`.
- **OpenAI Agents SDK adapter** (`charter.adapters.openai_agents`) with
  `charter_preflight()` and `@charter_gated` decorator. No hard
  dependency on `openai-agents` (lazy import).
- **fly.io deploy** workflow (`.github/workflows/deploy.yml`), gated on
  `vars.DEPLOY_ENABLED == 'true'`. `fly.toml` sets
  `CHARTER_LOG_FORMAT=json` for production.

### Tests

- 158 → 177 tests pass (+19).

## [0.6.0] — 2026-05-19 — Protocol completion

Closes out the v0 protocol surface: in-scope rewrite, loopback retry,
revoke / renew, discovery, structured logging, encrypted private keys.

### Added

- **`propose_within_scope(charter_url, intended_task, failed_verdict)`**
  MCP tool — single-shot LLM rewrite when an `incompatible` verdict has
  `rewrite_available=True`.
- **`propose_within_scope_verified(...)`** — loopback wrapper: generate at
  annealed temperature (0.2 → 0.5 → 0.8), grade against the Charter,
  retry up to `max_attempts` (default 3). Returns a `RewriteFailure`
  with full attempt history on giving up.
- **`charter revoke <principal> <agent>`** — flips status to `revoked`,
  re-signs.
- **`charter renew <principal> <agent>`** — issues a fresh Charter for
  the same binding with identical clauses + summary, new validity
  window, `replaces` ← old, old's `replaced_by` ← new, both re-signed.
  No LLM call.
- **Discovery** — `data/charters/index.json` + `resolve_charter_url`
  helper.
- **Structured logging** — `charter/_logging.py` with human + JSON
  formatters (`CHARTER_LOG_FORMAT=json`), every fetch path emits exactly
  one log line per outcome.
- **Encrypted private keys** — `CHARTER_KEY_PASSPHRASE` enables
  `BestAvailableEncryption` on `data/keys/<principal>.pem`. Loader
  detects encrypted vs plaintext from the PEM header so the failure
  modes are unambiguous.

### Tests

- 90 → 158 tests pass (+68).

## [0.5.0] — 2026-05-19 — Project hygiene + protocol foundations

First "landed open-source project" pass after the hackathon. Cleaned up
the repo, locked CI, added typed errors, and pinned the core protocol
contract.

### Added

- **Typed exception hierarchy** — `CharterError` base with
  `CharterNotFoundError`, `CharterSchemaError`, `CharterSignatureError`,
  `CharterExpiredError`, `CharterRevokedError`.
- **`TYPE_TO_DECISION`** protocol constant + aggregation rule
  (`incompatible > needs_approval > allow`, low-confidence fallback at
  `0.6`).
- **GitHub Actions CI** — `{py3.12, py3.13} × {ubuntu, macos, windows}`
  matrix, ruff + mypy + pytest.
- **Repo cleanup** — internal design notes moved out of the public tree;
  README rewritten for the open-source audience.

### Tests

- 0 → 90 tests pass.

## [v0 — hackathon prototype, pre-history]

The original 36-hour demo. Inline-only trust (TOFU), single signing
path, no transparency, one principal per process. Preserved at tag
`v0-demo` for historical reference.
