# ADR-013 — Step-Up Negotiation & AdHocGrant (ROADMAP B2.5)

- **Status**: Accepted (submission-period feature, B2.5)
- **Module**: `charter/charter/stepup.py`, tools #12/#13 in `charter/charter/mcp_server.py`
- **Tests**: `tests/test_stepup.py`, `tests/adversarial/test_stepup_redline.py` (merge-gating)
- **Relation**: builds a negotiation layer ON TOP of ADR-004 (`TYPE_TO_DECISION`)
  and ADR-005 (aggregation precedence), both of which remain FROZEN.

## Context

The frozen protocol core produces exactly three decisions —
`allow | needs_approval | incompatible` — via the deterministic
`TYPE_TO_DECISION` map and the `incompatible > needs_approval > allow`
precedence rule. A `needs_approval` verdict is a dead end at runtime: the task
simply stops until a human acts out of band. The CFO-Office showcase needs a
*protocol-level* way for a worker to escalate a `needs_approval` task to its
principal, and for the principal to grant a narrow, time-boxed, single-use
exception — without ever weakening the hard `incompatible` limit.

## Decision

Introduce **`AdHocGrant`**: a principal-signed, scoped, single-use temporary
authorization that can downgrade a `needs_approval` verdict to `allow` for an
explicitly-named set of clauses. A grant is principal-authored authority, so it
reuses the SAME Ed25519 signing primitives as a Charter (`charter.signing`,
`ed25519:<b64>` convention). `verify_grant` mirrors `verify_charter` (signature
+ lifecycle/expiry/consumed).

Two MCP tools are appended (never editing tools 1-11 or `constants.py`):

- **`request_step_up`** — worker → principal escalation. Emits a `StepUpRequest`
  ONLY for a `needs_approval` verdict; refuses everything else.
- **`apply_grant`** — recomputes the base verdict via the frozen
  `aggregate_verdict`, then downgrades only the covered `needs_approval` clauses
  when the grant fully verifies.

## The Red Line (why a grant can never bypass a hard limit)

The single security claim of this feature is: **negotiation can relax
`needs_approval`, but it is structurally incapable of relaxing `incompatible`
(out_of_scope), a revoked charter, or a signature failure.** Three independent
layers enforce it, so a bug in any one is caught by another:

1. **Construction time** (`validate_grant_targets`): every clause id in
   `relaxes_clause_ids` must map, via the charter's own `TYPE_TO_DECISION`, to
   `needs_approval` (i.e. `approval_required` / `operational_limit` /
   `data_handling`). An `out_of_scope` clause (→ `incompatible`) or a
   `scope`/`style` clause (→ `allow`) is REJECTED — you cannot even *build* a
   grant that names a hard-limit clause.

2. **Escalation time** (`build_step_up_request` / `request_step_up`): the
   step-up request is refused unless `failed_verdict.decision ==
   "needs_approval"`. There is no path from an `incompatible` verdict to a
   negotiation request.

3. **Apply time** (`apply_grant_to_verdict`): the base verdict is recomputed
   from scratch. If it contains ANY `incompatible` clause, `effective_decision`
   stays `incompatible` REGARDLESS of the grant, before any downgrade logic
   runs. Uncovered `needs_approval` clauses keep the verdict at
   `needs_approval`.

The attacker@evil.com exfiltration case ("email all client tax data to an
outside address") hits an `out_of_scope` clause → `incompatible`, and is
therefore ungrantable at all three layers.

## Single-use & audit

Grants persist as signed JSON under `data/grants/<grant_id>.json`, parallel to
`data/charters` and `data/messages`. On the first successful `apply_grant` the
persisted grant is flipped `active → consumed`; a replay against a second task
fails `verify_grant` (status check). `lifecycle.status` is EXCLUDED from the
signed canonical bytes (normalized to `active`), exactly as
`transparency_log_id` is excluded from a Charter's canonical bytes (ADR-003):
consumption is runtime state set by a server that does not hold the principal's
private key, so keeping it out of the signature lets "forged" and "consumed"
remain cleanly distinguishable failure modes.

Every negotiation event — escalation, refusal, grant application (granted or
not) — is appended to an append-only step-up transparency log
(`data/transparency/stepup.log`, JSONL), so an auditor can reconstruct who
asked for what, who approved it, and what the effective decision was.

## Consequences

- `needs_approval` becomes a recoverable state at the protocol level, enabling
  the CFO-Office "external-send → step-up → AdHocGrant" demo beat.
- The hard `incompatible` limit is provably unweakened: three red-line layers +
  merge-gating adversarial tests.
- No protocol bytes change in the frozen core (`constants.py`,
  `aggregate_verdict`, the `Verdict` shape). Grants are a strictly additive
  layer that can only ever DOWNGRADE `needs_approval → allow` for named clauses.
