# Changelog

All notable changes to Charter are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
