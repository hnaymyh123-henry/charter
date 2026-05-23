# 02 — Chain with budget

> **TL;DR.** Use an `operational_limit` clause for per-task budget. In a
> parent → child Charter Chain, the child may *tighten* the budget freely
> (Charter Chain v0.7 string subset rules intentionally don't gate
> `operational_limit`), and the chain-wide aggregate verdict will surface
> *every* fired budget clause so the caller can see which hop's cap was
> violated.

## Why this guide

A real corporate setup almost never lives at one hop. The org issues a
broad Charter to its assistant; the assistant issues narrower Charters to
sub-agents. When you want to express "we'll spend at most $1 per task at
the org level, but this sub-agent only gets $0.50," you need three things
to be true:

1. The budget has to live in a clause type that the aggregator knows to
   gate on (`operational_limit`, which maps to `needs_approval` per
   `TYPE_TO_DECISION` in `charter/constants.py`).
2. The child being *tighter* than the parent must not break chain
   verification.
3. When a task busts the budget, the chain-wide verdict must show
   *which* Charter (parent vs child) raised the flag.

This guide proves all three on a real two-hop chain.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- No API key required — the example builds Charters in-process.
- Read [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  first if you've never written a profile.yaml.

## Step-by-step

### 1. Encode the budget as an operational_limit clause

A profile field like:

```yaml
operational:
  budget_per_task_usd: 0.50
```

…is projected into a clause of `type: operational_limit` with text like:

> "Per-task budget cap is USD 0.50. Reject tasks projected to exceed this cost."

The clause type matters because the aggregator's TYPE_TO_DECISION map
(see [`charter/constants.py`](../../charter/constants.py)) routes
`operational_limit` to `needs_approval` — exactly what you want for a
budget guard: not a hard refusal, but "the principal has to confirm."

### 2. Build the chain

In [`examples/cookbook/02-chain-with-budget/main.py`](../../examples/cookbook/02-chain-with-budget/main.py)
we construct two Charters directly (skipping the profile.yaml indirection so
you can see the clause text verbatim):

```python
parent_clauses = [
    Clause(id="C-001", type="scope", text="Accounting and tax work"),
    Clause(id="C-002", type="scope", text="Engineering work"),
    Clause(id="C-101", type="out_of_scope", text="Marketing copy"),
    Clause(
        id="C-200",
        type="operational_limit",
        text="Per-task budget cap is USD 1.00. Reject tasks projected to exceed this cost.",
    ),
]

child_clauses = [
    Clause(id="C-001", type="scope", text="Accounting and tax work"),   # narrower
    Clause(id="C-101", type="out_of_scope", text="Marketing copy"),     # inherited verbatim
    Clause(
        id="C-200",
        type="operational_limit",
        text="Per-task budget cap is USD 0.50. Reject tasks projected to exceed this cost.",  # tighter
    ),
]
```

The child:

- **Drops `Engineering work`** from scope (narrower scope is the whole
  point of attenuation).
- **Inherits `Marketing copy`** as out_of_scope verbatim (required by
  chain v0.7's preservation rule).
- **Tightens the budget** from $1.00 to $0.50.

### 3. Verify the chain

```python
from charter.chain import verify_chain

assert verify_chain(child, parent)
```

The function checks three things (see `verify_chain` docstring in
[`charter/chain.py`](../../charter/chain.py)):

1. Every `out_of_scope` in the parent appears in the child (covered).
2. Every `approval_required` in the parent appears in the child (covered).
3. Every `scope` in the child appears in the parent (child does not
   invent capabilities).

`operational_limit` is *not* in that list. A tighter child budget passes
trivially.

### 4. Aggregate the verdict across the chain

When the calling agent's grader judges a task as a budget hit on *both*
Charters' C-200, the chain-wide aggregator applies
`incompatible > needs_approval > allow` across all matched clauses:

```python
chain_dump = [parent.model_dump(mode="json"), child.model_dump(mode="json")]
hits = {
    parent.charter_id: [{"id": "C-200", "hit": True, "confidence": 0.92, "reason": "..."}],
    child.charter_id:  [{"id": "C-200", "hit": True, "confidence": 0.95, "reason": "..."}],
}

from charter.mcp_server import aggregate_verdict_chain
fn = getattr(aggregate_verdict_chain, "fn", aggregate_verdict_chain)
verdict = fn(chain_dump, hits)
# verdict["decision"] == "needs_approval"
# verdict["matched_clauses"] has two entries, each carrying source_charter_id
```

The `source_charter_id` field on each `MatchedClause` is what tells the
caller "the *child*'s budget cap is the strict one." This is how you build
a UI that shows "$0.80 estimated, sub-agent cap is $0.50 → over-budget by
$0.30."

## Verification

Run:

```bash
python examples/cookbook/02-chain-with-budget/main.py
```

You should see (timestamps will differ):

```
========================================================================
Cookbook #02 -- Chain with budget
========================================================================

Built two-hop chain:
  [parent] charter:acme_corp:assistant_agent_v1:<DATE>
           operational_limit: budget USD 1.00/task
  [child]  charter:assistant_agent_v1:sub_agent_v1:<DATE>
           operational_limit: budget USD 0.50/task (TIGHTER)

verify_chain(child, parent) -> True
  Tighter budget is allowed: Charter Chain v0.7 string subset rules
  intentionally do not touch operational_limit, so the child may
  always set a stricter cap than the parent.

Task: 'Run a multi-hour reconciliation pass projected to cost USD 0.80'
  -> decision: NEEDS_APPROVAL
  -> applied clauses: ['<parent>::C-200', '<child>::C-200']
  -> reason: Aggregate decision 'needs_approval' from applied clauses across the chain: ...

[OK] Chain verified; budget clause propagated; chain verdict matches expectation.
```

The script will `assert` on both `chain_ok` and the decision. If either
fails, the script exits non-zero with a stack trace.

## Common pitfalls

- **Putting budget in `data_handling` instead of `operational_limit`.** The
  TYPE_TO_DECISION map only routes `operational_limit` to `needs_approval`;
  a budget-shaped clause typed as `data_handling` will *also* trigger
  `needs_approval` but for the wrong reason — your audit log will show "data
  handling violation" instead of "budget violation."
- **Expecting a `child < parent` budget to fail attenuation.** The opposite
  is true: tighter is always allowed. The reason chain v0.7 doesn't gate
  `operational_limit` is that comparing "budget USD 0.50" and "budget USD
  1.00" as strings would require numeric extraction — a hand-rolled
  numerical subset check is on the v0.8+ roadmap.
- **Forgetting that the grader runs *per Charter*.** When you call
  `aggregate_verdict_chain`, the `hits_per_charter` map keys by
  `charter_id`. If you only feed hits for the child, the parent's clauses
  silently contribute nothing — which is fine for "child catches it" cases
  but masks failures where the parent is the only one with the relevant
  clause. Always grade against every Charter in the chain.
- **Trying to "raise" the budget at the child.** A child clause saying
  `budget USD 2.00` would pass chain v0.7 verification (because
  `operational_limit` isn't gated) but is a *protocol smell*. The chain
  semantics assume tightening only. If you need the child to genuinely
  have more authority than the parent, you need a different parent
  Charter — not a chain.

## Related guides

- [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  — single-Charter foundations.
- [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md)
  — what happens to an in-flight chain when one hop revokes.
- [10 — Audit the transparency log](10-audit-transparency-log.md) — every
  Charter in the chain shows up in the transparency log; this is how you
  prove "the budget cap was set to $0.50 at this point in time."

## What's next

The MCP `fetch_charter_chain` tool (in
[`charter/mcp_server.py`](../../charter/mcp_server.py)) walks the chain over
HTTP, verifying each hop's signature + attenuation. The
`scripts/demo_chain.py` end-to-end demo wires that together with a live
in-process server — read it as the production-shape version of this
cookbook's in-process build.
