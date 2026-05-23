# 05 — Integrate OpenAI Agents SDK

> **TL;DR.** Charter ships with an OpenAI Agents adapter at
> `charter.adapters.openai_agents` (shipped in v0.7). Two entry points:
> `charter_preflight(...)` for one-call inline checks, and
> `@charter_gated(...)` as a decorator you wrap around any
> `@function_tool`. The decorator runs the preflight before each tool
> call and returns a refusal string to the LLM when the verdict is
> `incompatible` or `needs_approval`.

## Why this guide

[ADR-012](../decisions.md#adr-012--framework-adapter-优先级openai--anthropic-sdk--langgraph--crewai)
explicitly chose OpenAI Agents SDK as the first framework adapter to ship.
The reason: it's the model-vendor SDK with the highest current adoption,
and its `@function_tool` decorator gives us a clean wrap point. The
adapter is 200 LOC; the integration is a *one-line* decorator on each
tool.

This guide walks through both entry points, with a runnable example that
works *without* the OpenAI Agents SDK installed (so you can ship the
code on a fresh box and see it work) and also auto-runs the full SDK
demo when the SDK is present.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- For Layer 2 (full SDK demo): `pip install -e .[openai_agents]`.
- For the production grader: `ANTHROPIC_API_KEY` (the adapter's default
  grader is `charter.loopback._grade_via_llm` which calls Claude). Pass
  your own grader if you want to keep all LLM traffic on OpenAI — the
  cookbook example uses a hand-coded grader so it works fully offline.

## Step-by-step

### 1. Issue + host the Charter

The same as cookbook 01:

```bash
python examples/cookbook/01-write-charter-for-accountant/main.py
charter-server &
# Listens on http://localhost:8000
```

In the cookbook example for *this* guide, the script does both for you,
including bringing up `charter.server.app` on an ephemeral port in a
background thread.

### 2. Wire the decorator (the one-liner)

```python
from agents import Agent, Runner, function_tool
from charter.adapters.openai_agents import charter_gated

CHARTER_URL = "http://localhost:8000/alice@acme.com/research_agent_v1"

@charter_gated(CHARTER_URL)        # outer
@function_tool                     # inner: OpenAI Agents SDK
def accounting_task(task: str) -> str:
    return f"[accounting] done: {task}"
```

Apply `@charter_gated` *outside* of `@function_tool`. Why outside: the
SDK's `@function_tool` wraps the function in a tool spec for the LLM;
`@charter_gated` wraps the tool spec so the preflight runs *before* the
SDK invokes the underlying function. Inside-out, the preflight would
run after the tool has already been called.

When the LLM picks `accounting_task(...)`:

1. `charter_gated`'s wrapper runs `charter_preflight(CHARTER_URL, task)`.
2. The preflight: fetches the Charter, grades the task per clause,
   aggregates the verdict.
3. If `decision == "allow"`, the underlying function runs and its return
   value flows back to the LLM as usual.
4. If `decision in {"incompatible", "needs_approval"}`, the wrapper
   returns a human-readable refusal string to the LLM (something like
   `"Charter check returned incompatible. Applied clauses: ['C-002']. ..."`).
   The underlying function does NOT run.

### 3. Use `charter_preflight` directly when you don't want a decorator

For ad-hoc code paths that aren't an Agents SDK tool — a background job,
an HTTP handler, a Python script — call the function directly:

```python
from charter.adapters.openai_agents import charter_preflight

verdict = charter_preflight(CHARTER_URL, "Reconcile Q1 invoices")
if verdict.decision == "allow":
    do_the_work()
else:
    log_refusal(verdict)
```

The return is a `charter.schema.Verdict` — same shape as
`aggregate_verdict` produces. `verdict.matched_clauses` gives you the
per-clause breakdown including which one fired (`applied=True`).

### 4. Pass your own grader to avoid Anthropic calls

The default grader is `charter.loopback._grade_via_llm` (uses Anthropic).
To keep all LLM traffic on OpenAI, pass a custom grader:

```python
def my_openai_grader(charter, intended_task) -> list[dict]:
    """Call OpenAI Chat Completions with the GRADE_SYSTEM prompt from
    charter.prompts, parse the JSON response into the same shape:
    [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "..."}, ...]"""
    ...

@charter_gated(CHARTER_URL, hits_grader=my_openai_grader)
@function_tool
def accounting_task(task: str) -> str:
    ...
```

The cookbook example uses a substring-matching grader (zero LLM calls)
to keep the test fully offline.

## Verification

Run:

```bash
python examples/cookbook/05-integrate-openai-agents-sdk/main.py
```

You should see (in addition to log lines from the in-process server):

```
========================================================================
Cookbook #05 -- Integrate OpenAI Agents SDK
========================================================================

Charter published at: http://127.0.0.1:<PORT>/alice@acme.com/research_agent_v1

[layer 1] charter_preflight directly:
  task='Reconcile Q1 invoices'  -> allow
  task='Write a React component'  -> incompatible

[layer 1] charter_gated decorator:
  accounting_tool result: '[accounting] done: Reconcile Q1 invoices for tax prep.'
  code_tool result:       "Charter check returned incompatible. Applied clauses: ['C-002']. ..."

[layer 2] skipped: openai-agents not installed. `pip install -e .[openai_agents]` to enable.

[OK] charter_preflight allowed scope tasks and refused out-of-scope tasks.
```

If you `pip install -e .[openai_agents]` and rerun, Layer 2 also runs
the full SDK demo from `examples/openai_agents_demo.py` (which needs an
`OPENAI_API_KEY` to actually fire the Runner; without one, the SDK
itself errors with a clear message).

Checks:

1. Layer 1 preflight returns `allow` for the accounting task and
   `incompatible` for the React task.
2. The decorator path: `accounting_tool` returns the body's string;
   `code_tool` returns the wrapper's refusal string (with `C-002` listed
   as applied).
3. Cookbook script asserts all three.

## Common pitfalls

- **Decorator order inverted.** `@function_tool` outside, `@charter_gated`
  inside means the SDK wraps the wrapper — and the SDK calls the wrapper
  through its tool-spec machinery, where the wrapper's preflight runs
  too late (after the LLM has already decided to call). Keep
  `@charter_gated` outermost.
- **Different `charter_url` per binding, same Charter file.** The
  decorator binds *one* `charter_url` per tool. If your agent acts for
  multiple principals, you need one decorated copy of the tool per
  binding, or you need to use `charter_preflight` inline with the
  current principal's URL.
- **`refuse_on` default catches `needs_approval`.** By design — most
  callers want approval-required tasks to surface to a human before the
  tool runs. If your tool body itself implements an approval workflow,
  override: `@charter_gated(URL, refuse_on=("incompatible",))`. Then
  `needs_approval` falls through into the body.
- **Using the default grader without an API key.** Without
  `ANTHROPIC_API_KEY` the default grader raises. Pass your own grader
  for offline / OpenAI-only setups.
- **The wrapper returns a string, the LLM sees the string.** When the
  Charter blocks, the *LLM* receives the refusal string as the tool's
  output and decides what to say to the user. This is intentional: the
  protocol's choice is "let the LLM mediate the explanation," not "raise
  an exception the SDK doesn't know what to do with."

## Related guides

- [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  — issue the Charter you're about to gate against.
- [06 — Integrate Anthropic SDK](06-integrate-anthropic-sdk.md) — the
  Anthropic-side counterpart (full adapter pending; this guide describes
  the planned hook points).
- [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md)
  — the decorator runs `charter_preflight` *every* call by default; pair
  it with the ticket pattern for long-running tools.

## What's next

The full adapter source is at
[`charter/adapters/openai_agents.py`](../../charter/adapters/openai_agents.py).
The minimal full demo using the SDK Runner is at
[`examples/openai_agents_demo.py`](../../examples/openai_agents_demo.py)
— it's the same shape this cookbook's example invokes when the SDK is
installed. Both are kept under 100 LOC each so you can copy and adapt.
