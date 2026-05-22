# 01 — Write a Charter for an accountant

> **TL;DR.** A profile.yaml is the only input you have to write by hand. One
> `charter issue` (or the offline `scripts/seed_demo.py` shortcut) turns it
> into a signed, fetchable Charter that any calling agent can verify. This
> guide walks through the whole loop for Alice, a senior accountant at Acme
> Corp, in under five minutes.

## Why this guide

The first question every new Charter user asks is "what does a real Charter
even look like, and where do I start?" The protocol doc explains the schema,
but the schema is the *output*. The *input* is a 10-field YAML — and most of
the trouble new users hit is in writing that YAML, not in the rest of the
pipeline.

This guide walks through:

- What goes into each of the 10 profile fields, with a real Acme example.
- How `charter issue` turns the profile into clauses, signs them, and writes
  the Charter to disk.
- How to verify the result with `charter inspect` (or programmatically, by
  re-reading from disk).

After this you should be able to write a profile for *your own* principal
in 10 minutes.

## Prerequisites

- Python 3.12+.
- A clone of the Charter repo with `pip install -e .` already done.
- For this guide we'll use the offline seed (no API key needed). If you want
  the *real* `charter issue` path, set `ANTHROPIC_API_KEY` in `.env`.

## Step-by-step

### 1. Draft the profile.yaml

Open [`examples/cookbook/01-write-charter-for-accountant/profile.yaml`](../../examples/cookbook/01-write-charter-for-accountant/profile.yaml).
The top of it looks like this:

```yaml
principal:
  id: alice@acme.com
  role: Senior Accountant at Acme Corp, focused on tax season Q1-Q2

agent:
  id: research_agent_v1
  card_url: https://agents.example.com/research_agent_v1/card.json

scope:
  - 会计
  - 报税
  - 做账
  - 财务分析
  - 发票分类
  - 税务凭证整理

out_of_scope:
  - 营销文案
  - 广告策划
  - 写代码
  - UI 设计
```

Each field carries one specific responsibility:

- **`principal.id`** is the *identity* of whoever the agent is acting for —
  the boundary the Charter draws. Email-shaped strings are conventional but
  any stable identifier works.
- **`agent.id`** is the agent instance, *not* the model. Two principals can
  legitimately point at the same `agent.id`; Charter computes a different
  binding per principal, so each gets a different Charter.
- **`scope`** / **`out_of_scope`** are short noun phrases. Verbs help
  (`报税`, `发票分类`), but keep each phrase to one concrete capability.
- **`approval_required`** is the "humans must say yes" list. Be explicit
  about what triggers the approval gate — `单笔金额超过 $10,000 的财务建议`
  is concrete; `重要事项` is not.
- **`data_handling`** is a single object with `what` (the data classes) and
  `rules` (the obligations). Keep both grounded in real data: `客户的报税资料、
  税号、银行流水` not `敏感信息`.
- **`operational`** is where you put hours, per-task budget, monthly cap. The
  projection step turns these into one `operational_limit` clause.
- **`style`** is a free-form sentence the projector folds into a single
  `style` clause.
- **`lifecycle.valid_days`** controls how long the Charter is good for. The
  default is 30; longer for stable roles, shorter for one-off engagements.

For more on the *phrasing* of each field, see
[07 — Profile YAML best practices](07-profile-yaml-best-practices.md).

### 2. Issue the Charter

Two paths to get from profile.yaml to a signed Charter:

**Live (one LLM call):**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
charter issue examples/cookbook/01-write-charter-for-accountant/profile.yaml
```

This runs `charter.projection.project()` which calls Claude once to expand
the profile into typed clauses, signs the result with Alice's Ed25519 key
(auto-generated on first use), and writes the Charter to
`data/charters/alice_at_acme.com__research_agent_v1.json`.

**Offline (no LLM, deterministic clauses):**

```bash
python examples/cookbook/01-write-charter-for-accountant/main.py
```

This goes through `scripts.seed_demo.seed_from_profile`, which substitutes
hand-curated clauses for the projector call but otherwise runs the exact
same sign + save code path. The Charter file produced is byte-for-byte
indistinguishable from one issued live. We use this throughout the cookbook
so the samples have no hidden API-key dependency.

### 3. Inspect the Charter

```bash
charter inspect alice@acme.com research_agent_v1
```

The CLI pretty-prints the binding, validity window, summary, and every
clause with its `type` color-coded. The example script in step 2 does the
same thing inline (it re-reads the just-written Charter from disk and
prints a summary).

### 4. Host the Charter (optional, for fetch tests)

If you want a calling agent to fetch this Charter, run:

```bash
charter-server
# Listens on http://localhost:8000
```

…then `GET http://localhost:8000/alice@acme.com/research_agent_v1` returns
the Charter JSON. This is the URL form a calling agent passes to the MCP
`fetch_charter` tool.

## Verification

Running `python examples/cookbook/01-write-charter-for-accountant/main.py`
should print (your timestamps and hashes will differ):

```
========================================================================
Cookbook #01 -- Write a Charter for an Accountant
========================================================================

[1/3] Loaded:    .../profile.yaml  ->  alice@acme.com
[2/3] Projected: 7 clauses (seeded, no LLM call)
[3/3] Signed:    ed25519, public key embedded in provenance

[OK] Charter active
  charter_id:  charter:alice@acme.com:research_agent_v1:<DATE>
  binding:     alice@acme.com x research_agent_v1
  valid_until: <DATE>T...+00:00
  file:        data/charters/alice_at_acme.com__research_agent_v1.json
  url:         http://localhost:8000/alice@acme.com/research_agent_v1

--- inspect (re-read from disk) ---
charter_id: charter:alice@acme.com:research_agent_v1:<DATE>
binding:    alice@acme.com x research_agent_v1
valid:      <DATE> -> <DATE> (active)
summary:    This agent acts for Alice's accounting, tax, and bookkeeping ...

clauses (7):
  C-001  scope               This agent acts for Alice's ...
  C-002  out_of_scope        Do not accept marketing copy, ...
  ...
[OK] Charter written, re-read, and inspected. Done.
```

Checks to do:

1. The file exists at `data/charters/alice_at_acme.com__research_agent_v1.json`.
2. The JSON parses; `lifecycle.status` is `"active"`; `provenance.issuer_signature`
   starts with `"ed25519:"`.
3. If you ran `charter-server`, `curl http://localhost:8000/alice@acme.com/research_agent_v1`
   returns the same JSON.

## Common pitfalls

- **Forgot `ANTHROPIC_API_KEY` for `charter issue`.** `charter issue` will
  exit immediately telling you to set it. Either set it, or use the offline
  seed path (`python examples/cookbook/01-write-charter-for-accountant/main.py`).
- **The `agent.id` collision panic.** It's *intentional* that two profiles
  with the same `agent.id` produce two different Charters — Charter is
  keyed on the `(principal_id, agent_id)` pair, not on `agent_id` alone.
  See `profiles/alice.yaml` and `profiles/bob.yaml` for the canonical
  illustration: both bind to `research_agent_v1`, with very different scope.
- **Phrasing the `out_of_scope` list as long sentences.** The LLM grader
  works best when each entry is a noun phrase about one capability. Use
  `广告策划` not `禁止帮我们做广告策划之类的事情`. See
  [07 — Profile YAML best practices](07-profile-yaml-best-practices.md).
- **Putting per-task budget in `data_handling`.** Budget belongs in
  `operational` — that's where the projector knows to emit an
  `operational_limit` clause with the budget cap. Wrong placement gives you
  a `data_handling` clause that the grader can't match on cost-related
  hits.
- **Using `principal.id` as a display name.** It's used as a directory key
  on disk (`data/charters/alice_at_acme.com__...`) and as part of the
  charter_url. Stick to stable, file-safe identifiers; cosmetic display goes
  into `principal.role`.

## Related guides

- [02 — Chain with budget](02-chain-with-budget.md) — uses an
  `operational_limit` clause across a parent → child chain.
- [07 — Profile YAML best practices](07-profile-yaml-best-practices.md) —
  how to phrase clauses so the LLM grader stays accurate.
- [08 — Self-host `.well-known/charter`](08-self-host-well-known.md) — once
  you have a Charter, where to publish it.

## What's next

Once you have a Charter on disk you can:

- Fetch it from a calling agent via MCP (`fetch_charter` tool in
  [`charter/mcp_server.py`](../../charter/mcp_server.py)).
- Wire it into an OpenAI Agents tool using `@charter_gated` (see
  [05 — Integrate OpenAI Agents SDK](05-integrate-openai-agents-sdk.md)).
- Renew, revoke, or chain it (see [`charter/cli.py`](../../charter/cli.py)
  and [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md)).
