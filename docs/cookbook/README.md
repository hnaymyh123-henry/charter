# Charter Cookbook

> Scenario-driven how-to guides. PRODUCT.md tells you what Charter **is**; the
> Cookbook tells you what to **do** with it on a Monday morning. Each guide is
> self-contained and includes a runnable code sample under
> [`examples/cookbook/`](../../examples/cookbook/).

## When to read which guide

| Need | Read |
|---|---|
| "How do I write a Charter for my own role?" | [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md) |
| "How do I propagate a per-task budget through a multi-hop delegation?" | [02 — Chain with budget](02-chain-with-budget.md) |
| "How do I revoke without nuking in-flight work?" | [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md) |
| "How do I gate a Stripe payment behind a Charter check?" | [04 — Integrate Stripe](04-integrate-stripe.md) |
| "How do I plug Charter into an OpenAI Agents SDK tool?" | [05 — Integrate OpenAI Agents SDK](05-integrate-openai-agents-sdk.md) |
| "How do I plug Charter into the Anthropic SDK?" | [06 — Integrate Anthropic SDK](06-integrate-anthropic-sdk.md) |
| "How should I word my profile.yaml clauses so the grader doesn't go wild?" | [07 — Profile YAML best practices](07-profile-yaml-best-practices.md) |
| "How do I host my own Charters at `myco.com/.well-known/charter/…`?" | [08 — Self-host `.well-known/charter`](08-self-host-well-known.md) |
| "How do I publish a JWKS and rotate keys without breaking callers?" | [09 — Deploy a JWKS and rotate keys](09-deploy-jwks.md) |
| "How do I prove no one tampered with a Charter using the transparency log?" | [10 — Audit the transparency log](10-audit-transparency-log.md) |

## Conventions

Every guide follows the same skeleton:

1. **TL;DR** — three sentences on what the guide does.
2. **Why this guide** — the real-world pain it solves.
3. **Prerequisites** — what you must have installed / configured first.
4. **Step-by-step** — copy-paste-able commands and code.
5. **Verification** — exactly what success looks like in your terminal.
6. **Common pitfalls** — the mistakes I hit on first attempt.
7. **Related guides** — neighbors in the cookbook.
8. **What's next** — where to look if you want to go deeper.

Code samples live under `examples/cookbook/<NN-name>/`. Each has its own
`main.py` and (when relevant) `profile.yaml`. The samples only depend on
the Python standard library, `charter` (this package), and at most one
extra third-party library named at the top of each example.

## What this cookbook does NOT cover

- API reference for protocol fields (Charter schema, Verdict shape, etc.).
  Read [`PRODUCT.md`](../../PRODUCT.md) §4 instead.
- Architecture rationale (why TYPE_TO_DECISION is a protocol constant, why
  signatures cover `transparency_log_id` only partially, etc.). Read
  [`PRODUCT.md`](../../PRODUCT.md) §5 and [`docs/decisions.md`](../decisions.md).
- Adapter pages for frameworks Charter does not target. Per
  [ADR-012](../decisions.md#adr-012--framework-adapter-优先级openai--anthropic-sdk--langgraph--crewai),
  cookbook adapter guides cover OpenAI Agents SDK (shipped v0.7) and
  Anthropic SDK (template only; full adapter deferred); LangGraph and
  CrewAI are intentionally out of scope.
