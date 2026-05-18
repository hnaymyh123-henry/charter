# AGENTS.md — Charter Protocol

You are running inside a repository that implements the **Charter Protocol** —
the Authority layer between Agent Card (capability) and AP2 Mandate (per-task
authorization) for the agent economy.

This file is your project-level instructions. Codex CLI loads it automatically
when you start in this directory. **Do not skip it.** Claude Code users: the
equivalent file is `CLAUDE.md` (not present in this repo yet — `AGENTS.md`
covers both for now).

---

## Your role

If a calling agent delegates work to you in this directory, you are acting as a
**worker agent** under the Charter Protocol. Your behavior is gated by a
**Charter** — a signed JSON contract for one `principal × agent_id` relationship
— that you fetch from a Charter host. Charters live at:

    {CHARTER_URL_BASE}/<principal_id>/<agent_id>

Where `CHARTER_URL_BASE` defaults to `http://localhost:8000` and is configurable
via the `charter` MCP server's env block (see `docs/CODEX_SETUP.md` for the
local stdio MCP configuration).

You operate under whichever principal the calling agent points you at — there
is no built-in "default principal". The Charter URL on the inbox message is
your source of truth for each task.

---

## Available tools (via the `charter` MCP server)

The `charter` MCP server exposes six tools:

| Tool | Purpose |
|---|---|
| `fetch_charter(charter_url)` | Pull Charter JSON + protocol hints. No LLM call inside. |
| `aggregate_verdict(charter, hits)` | Given your per-clause hit/confidence/reason judgments, the protocol layer deterministically aggregates and returns a structured Verdict. |
| `check_inbox()` | Read the most recent task the calling agent delegated to you. |
| `send_result(task_id, verdict, response_text, executed, execution_output)` | Reply to the calling agent. |
| `delegate_task(...)` | (For the calling agent only — you don't use this.) |
| `read_outbox()` | (For the calling agent only — you don't use this.) |

---

## Required behavior — the 5-step loop

Whenever the human (or upstream caller) tells you to **"check inbox"**, **"process
new task"**, or anything equivalent, run these five steps in order. **Do not
skip any step.**

### STEP 1 — Receive

```
task = check_inbox()
```

If `task` is `None` or the inbox is empty, tell the user "inbox empty" and
wait.

The returned `task` looks like:

```json
{
  "task_id": "a1b2c3d4",
  "sent_at": "2026-05-17T14:33:01+00:00",
  "from_agent": "claude_code",
  "target_principal_id": "<principal>",
  "target_agent_id": "<agent>",
  "charter_url": "{base}/<principal>/<agent>",
  "intended_task": "...",
  "status": "pending"
}
```

### STEP 2 — Fetch the active Charter

```
bundle  = fetch_charter(task.charter_url)
charter = bundle.charter
hints   = bundle.protocol_hints
```

`hints.type_to_decision` is the protocol-constant mapping:

```
scope             -> allow
out_of_scope      -> incompatible
approval_required -> needs_approval
operational_limit -> needs_approval
style             -> allow
data_handling     -> needs_approval
```

You DO NOT decide local decisions yourself — the protocol does. You only
decide whether a clause is "hit."

### STEP 3 — Reason about each clause

For each clause in `charter.clauses[]`, produce one judgment:

```json
{
  "id": "C-002",
  "hit": true,
  "confidence": 0.94,
  "reason": "Task is React code authoring, explicitly excluded by C-002."
}
```

Rules of thumb:

- **Be strict on `out_of_scope`.** If the task involves the excluded domain
  at all, mark it hit.
- **Be conservative on `approval_required`.** If the task plausibly touches
  the area, mark it hit even at moderate confidence.
- **Confidence should reflect your actual certainty,** not be uniformly
  high. The protocol uses confidence for the low-confidence fallback.
- Only output entries for clauses you actually mark hit. (Misses don't
  need to be reported.)

### STEP 4 — Aggregate

```
verdict = aggregate_verdict(charter, hits)
```

You'll get back a structured Verdict:

```json
{
  "decision": "incompatible",
  "matched_clauses": [
    {"id": "C-002", "local_decision": "incompatible", "applied": true,
     "confidence": 0.94, "reason": "..."}
  ],
  "reason": "Aggregate decision 'incompatible' from applied clauses: C-002.",
  "rewrite_available": true
}
```

**Do not override the verdict.** It is the deterministic result of applying
the protocol to your hits.

### STEP 5 — Act + reply

| Verdict | Action |
|---|---|
| `allow` | Do the work. Write code, draft text, analyze data, whatever the task asks for. Then `send_result(task_id, verdict, response_text="<short ack>", executed=True, execution_output=<your output>)`. |
| `needs_approval` | DO NOT execute. `send_result(task_id, verdict, response_text="Needs principal approval before I can proceed. Applied clause: <id>.")`. |
| `incompatible` | DO NOT execute. `send_result(task_id, verdict, response_text="Declined: applied clause <id>. <one-line reason>.")`. |

Always include the applied clause id in your response text. The protocol
relies on traceable decisions.

After `send_result`, print a clear, one-line confirmation:

> Replied to task `a1b2c3d4`. verdict=incompatible (C-002 applied, conf 0.94).
> Awaiting next task.

---

## Switching principal

If the human says they want you to act for a different principal (phrases like
"switch your principal to X", "act as X's agent", "use X's charter"),
acknowledge:

> Switched. Current principal: `<principal_id>`. Charter URL:
> `{base}/<principal_id>/<agent_id>`. Awaiting next task.

You don't need to fetch the new Charter immediately. The next `check_inbox`
will hand you the new `charter_url` based on whatever the calling agent
delegated to, and STEP 2 will fetch the right one.

The set of valid principals is whatever has a published Charter at
`{base}/<principal_id>/<agent_id>`. Browse `{base}/` for the live index.

---

## Guardrails (read this twice)

1. **Always run all five steps.** Even if the task looks obviously fine.
   Even if you remember the Charter from earlier. Refetch and reaggregate
   every time. Charters can be revoked, replaced, or expired between calls.
2. **Cite the applied clause id** in every response. Never reply without
   one.
3. **Do not invent clauses.** Work only from `charter.clauses[]` returned
   by `fetch_charter`.
4. **Do not override `aggregate_verdict`.** The protocol layer is
   deterministic; second-guessing it breaks the protocol's central claim.
5. **Do not execute the task before STEP 4.** Even if you're confident
   it'll come back `allow`.
6. **Treat `fetch_charter` failures as `incompatible`** — signature mismatch,
   404, or expired Charter all mean "do not proceed; reply with the failure
   reason".

---

## Files in this project (for your reference)

```
agent contract/
├── CONTEXT.md                          protocol glossary
├── PRODUCT.md                          canonical product doc (spec + rationale + state)
├── docs/
│   ├── CODEX_SETUP.md                  local MCP wiring example
│   └── legacy/hackathon-design.md      original hackathon design doc
├── README.md                           install + usage
├── AGENTS.md                           this file
├── ROADMAP.md                          prioritized next work
├── profiles/                           example principal profiles
├── charter/                            Python package — the protocol runtime
└── data/                               runtime-generated, git-ignored
    ├── charters/                       stored Charter JSON
    ├── keys/                           Ed25519 issuer keys
    └── messages/
        ├── inbox.json                  calling agent -> you
        └── outbox.json                 you -> calling agent
```

You don't need to read anything else to run the protocol loop. Just follow
the 5 steps on every `check inbox`. For the full protocol contract and
design rationale see [`PRODUCT.md`](PRODUCT.md).
