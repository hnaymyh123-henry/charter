# 06 — Integrate Anthropic SDK

> **TL;DR.** Charter does not yet ship a turnkey Anthropic adapter
> (see [ADR-012](../decisions.md#adr-012--framework-adapter-优先级openai--anthropic-sdk--langgraph--crewai)).
> Until it does, you can build a local equivalent of `@charter_gated` in
> ~50 LOC and hook it at the *tool-execution* boundary of an Anthropic
> tool-use loop. This guide shows the recommended hook points and ships a
> runnable example with a fake Anthropic SDK so you can see the pattern
> end-to-end today.

## Why this guide

The protocol layer is framework-neutral by design — `fetch_charter` and
`aggregate_verdict` are MCP tools, not Python adapters. But every
mainstream agent framework has a *tool-execution* boundary, and that's
the natural place to drop the Charter check. For OpenAI Agents we shipped
[`charter.adapters.openai_agents`](../../charter/adapters/openai_agents.py)
in v0.7. For Anthropic / Claude Agent SDK we have a planned adapter on
the backlog but it's not built yet.

Three reasons you might want this guide anyway:

1. You're already on the Anthropic SDK and want to integrate Charter
   *now*, not when the adapter ships.
2. You're building a custom multi-agent system on top of `anthropic` and
   want a stable hook point to retrofit Charter into.
3. You want to write the adapter PR yourself — the example here is the
   shape we'll ship.

> **Privacy note.** When the Anthropic adapter does ship, it will use the
> *same* clause-text grading as the OpenAI adapter — the calling agent's
> LLM gets to see every clause body. That's fine for v0 but breaks down
> when clauses contain sensitive values (e.g. a per-customer budget cap).
> The fix is **redaction + SD-JWT selective disclosure** — this is
> [ADR-011](../decisions.md#adr-011--隐私层走分层方案不一步到位)'s "path 1",
> currently planned in
> [Issue #31](https://github.com/hnaymyh123-henry/charter/issues/31) and
> *in-flight*, **not yet supported**. Until then, treat the Charter as
> entirely public — anything you put in a clause is visible to every
> calling agent.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- For the production version: `pip install anthropic` and
  `ANTHROPIC_API_KEY` set.

## Step-by-step

### 1. Identify the tool-execution boundary

The Anthropic agent loop, at its smallest, looks like this (paraphrased):

```python
import anthropic
client = anthropic.Anthropic()

messages = [{"role": "user", "content": "..."}]
while True:
    resp = client.messages.create(model="claude-sonnet-4-6", messages=messages, tools=TOOLS)
    if resp.stop_reason != "tool_use":
        break
    for block in resp.content:
        if block.type == "tool_use":
            tool_fn = TOOL_TABLE[block.name]
            result = tool_fn(**block.input)            # <-- THIS LINE
            messages.append({"role": "tool", ..., "content": result})
```

The Charter check goes right before `tool_fn(**block.input)`. Same place
the OpenAI Agents `@charter_gated` decorator hooks into.

### 2. Write a `charter_gated_tool` decorator

The recommended shape (lifted verbatim from
[`examples/cookbook/06-integrate-anthropic-sdk/main.py`](../../examples/cookbook/06-integrate-anthropic-sdk/main.py)):

```python
def charter_gated_tool(*, principal_id, agent_id, grader, refuse_on=("incompatible", "needs_approval")):
    def decorator(fn):
        def wrapper(*args, **kw):
            task = str(args[0]) if args else kw.get("task", "")
            verdict = charter_preflight_inline(
                principal_id=principal_id, agent_id=agent_id,
                task=task, grader=grader,
            )
            if verdict["decision"] in refuse_on:
                applied = [m["id"] for m in verdict.get("matched_clauses", []) if m["applied"]]
                return (
                    f"Charter check returned {verdict['decision']}. "
                    f"Applied clauses: {applied}. {verdict.get('reason', '')}"
                )
            return fn(*args, **kw)
        return wrapper
    return decorator
```

Then wrap your tools:

```python
@charter_gated_tool(principal_id="alice@acme.com", agent_id="research_agent_v1", grader=my_grader)
def reconcile(task: str) -> str:
    return run_reconciliation(task)
```

### 3. The preflight body

`charter_preflight_inline` mirrors what the future
`charter.adapters.anthropic.charter_preflight` will do:

```python
def charter_preflight_inline(*, principal_id, agent_id, task, grader):
    from charter.mcp_server import aggregate_verdict
    from charter.storage import load_charter  # or fetch_charter over HTTP

    charter = load_charter(principal_id, agent_id)
    hits = grader(charter, task)
    fn = getattr(aggregate_verdict, "fn", aggregate_verdict)
    return fn(charter.model_dump(mode="json"), hits)
```

Three pieces:

1. **Load the Charter.** From disk in the cookbook (`load_charter`); in
   production, prefer `_fetch_and_verify(charter_url)` for the
   signature+JWKS+pin checks.
2. **Grade the task per clause.** The grader is your responsibility —
   replace the cookbook's substring matcher with one `anthropic.messages.create`
   call using the `GRADE_SYSTEM` prompt from
   [`charter/prompts.py`](../../charter/prompts.py).
3. **Aggregate.** `aggregate_verdict` is a FastMCP-decorated function;
   `getattr(..., "fn", ...)` unwraps it for direct calling.

### 4. Wire the gate into your tool loop

Once your tools are decorated, the agent loop is unchanged:

```python
while resp.stop_reason == "tool_use":
    for block in resp.content:
        if block.type == "tool_use":
            tool_fn = TOOL_TABLE[block.name]
            result = tool_fn(**block.input)   # gate runs inside the decorator
            messages.append({"role": "tool", "tool_use_id": block.id, "content": result})
    resp = client.messages.create(...)
```

When Charter blocks a call, `result` is the refusal string. The next
turn's model call sees it as the tool's output and decides what to say
to the user.

## Verification

Run:

```bash
python examples/cookbook/06-integrate-anthropic-sdk/main.py
```

Expected output (timestamps will differ):

```
========================================================================
Cookbook #06 -- Charter-gate Anthropic / Claude Agent SDK calls
========================================================================

Charter on disk for alice@acme.com x research_agent_v1.

step=0 tool='reconcile'
  input:  {'task': 'Reconcile Q1 invoices for tax prep.'}
  output: '[accounting] reconciled: Reconcile Q1 invoices for tax prep.'

step=1 tool='write_code'
  input:  {'task': 'Write a React component for the table.'}
  output: "Charter check returned incompatible. Applied clauses: ['C-002']. ..."

[OK] Gate routed an in-scope tool through and refused an out-of-scope tool.
```

Checks:

1. The reconcile tool ran the body (output starts with `[accounting]`).
2. The write_code tool was refused (output starts with `Charter check
   returned`).
3. The script asserts both via `assert "reconciled" in ...` and
   `assert "Charter check returned" in ...`.

## Common pitfalls

- **Hooking too early in the agent loop.** The check has to run *for
  each tool invocation*, not once at agent startup. A single
  `charter_preflight(...)` at the top of `agent.run()` would let any
  subsequent tool slip through under whatever the first task's verdict
  happened to be. The decorator scope is one tool call.
- **Forgetting that the LLM sees the refusal.** The wrapper returns a
  string; the SDK feeds the string back to the model as the tool's
  output. This is by design — let the model explain to the user. If you
  want to *also* surface the refusal in your logs, log it inside the
  wrapper before returning.
- **Treating `needs_approval` as `allow`.** Anthropic's tool-use loop has
  no native "approval pending" concept. By default `refuse_on` includes
  `needs_approval`, so the model gets a refusal string and can choose to
  ask the user. If you have an out-of-band approval queue, override
  `refuse_on=("incompatible",)` and route `needs_approval` into your
  queue from inside the tool body.
- **Mixing `tool_use` blocks across decorated and undecorated tools.**
  An agent with three tools where only two are gated is a capability hole.
  Decorate every tool, even the ones that "couldn't possibly need a
  check" — Charter clauses can change over time, and the unconditional
  gate is what makes audit possible.
- **Calling `anthropic.Anthropic()` inside the gate to grade.** Fine for
  prototyping, but every preflight then costs one extra Claude call. For
  cost-sensitive deployments, cache the verdict per `(charter_id, task)`
  pair using the in-flight ticket pattern from
  [cookbook 03](03-revoke-without-breaking-inflight.md).

## Related guides

- [05 — Integrate OpenAI Agents SDK](05-integrate-openai-agents-sdk.md) —
  the shipped sibling adapter; the Anthropic version will mirror its API.
- [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  — issue the Charter you're about to gate against.
- [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md)
  — the ticket pattern for tools whose body takes longer than a single
  preflight round-trip can be cached for.

## What's next

- When the real Anthropic adapter ships, it will live at
  `charter/adapters/anthropic.py` and export `charter_preflight` +
  `charter_gated` with the same signatures as the OpenAI version. The
  cookbook's `charter_preflight_inline` and `charter_gated_tool` are
  drop-in replacements until then — just swap the imports.
- ADR-011 path 1 (redaction + SD-JWT) is the privacy-layer upgrade that
  will let you ship clauses with sensitive values without exposing them
  to every caller's LLM. The work is in-flight on
  [Issue #31](https://github.com/hnaymyh123-henry/charter/issues/31); this
  cookbook's clause text is intentionally non-sensitive so today's
  public-clause model is safe to use.
