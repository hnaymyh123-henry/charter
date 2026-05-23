# 03 — Revoke without breaking in-flight tasks

> **TL;DR.** `charter revoke` is instant — the next `fetch_charter` raises
> `CharterRevokedError`. To keep in-flight work alive across a revocation,
> the calling agent caches the *verdict* (not the Charter) per task and
> trusts the cached verdict until the task completes. The grace period is
> implicit: it lasts exactly as long as the in-flight task does, no
> wall-clock timer required.

## Why this guide

The naive revoke pattern looks like this:

1. Worker fetches Charter, verdict says `allow`, starts working.
2. Principal hits "revoke" mid-execution.
3. Worker re-fetches the Charter on the next loop iteration, gets
   `CharterRevokedError`, hard-aborts the task — half-finished work in the
   wild.

The fix is small but easy to get wrong: cache the verdict *per task*, not
the Charter globally. Cached verdicts are bound to a `task_id`, so a
revocation cannot retroactively invalidate work that is already in flight,
but *also* cannot leak into new tasks (a new `task_id` always re-fetches).

This guide shows the pattern, walks through the four critical moments
(issue / revoke / new-task / in-flight-task), and explains the trade-off
between "trust the ticket" (the basic version) and "re-fetch before the
irreversible step" (the strict version).

## Prerequisites

- Python 3.12+, `pip install -e .`.
- No API key required.

## Step-by-step

### 1. Keep an in-flight registry keyed by task_id

The data structure is a one-page dataclass:

```python
@dataclass
class InFlightTicket:
    task_id: str
    charter_id: str
    verdict_decision: str  # "allow" / "needs_approval" / "incompatible"
    captured_at: datetime
```

…and a `dict[str, InFlightTicket]` in your job queue / workflow engine.
Real systems persist this with the task state so a worker restart can
resume from the same ticket.

### 2. Open a ticket when the task starts

```python
def begin_task(registry, *, principal_id, agent_id, task_id, task_description):
    charter = load_charter(principal_id, agent_id)  # or fetch_charter over HTTP
    if charter.lifecycle.status == "revoked":
        raise CharterRevokedError(...)
    # run grader, aggregate verdict, decision = "allow"
    return registry.open(task_id, charter.charter_id, decision)
```

The grader call is unchanged; what's new is `registry.open(...)`.

### 3. Use the ticket — not the Charter — at execution time

```python
def complete_task(registry, *, task_id):
    ticket = registry.get(task_id)
    if ticket.verdict_decision != "allow":
        return "refused (cached verdict was not allow)"
    # Run the actual work. Do NOT re-fetch the Charter.
    return "ok"
```

This is the line that lets revocation be non-disruptive: the cached ticket
is the source of truth for *this task*, even if the Charter has flipped to
`revoked` in the meantime.

### 4. After the task completes, drop the ticket

```python
registry.close(task_id)
```

After this, no new task can ride the old verdict — fresh tasks always
start at step 2 (`begin_task`), which re-reads the (now-revoked) Charter
and refuses.

## Verification

Run:

```bash
python examples/cookbook/03-revoke-without-breaking-inflight/main.py
```

You should see (timestamps will differ):

```
========================================================================
Cookbook #03 -- Revoke without breaking in-flight tasks
========================================================================

t0  Charter issued: charter:alice@acme.com:research_agent_v1:<DATE>
  [verdict] task='task_001' decision=allow
     in-flight ticket opened: task_id=task_001

t1  Charter revoked: charter:alice@acme.com:research_agent_v1:<DATE>

t2  new task refused: CharterRevokedError: Charter charter:alice@acme.com:research_agent_v1:<DATE> is revoked

  [execute] task='task_001' using cached verdict from t0
t3  in-flight ok: task task_001 completed under verdict captured at <TS>

[OK] Revocation refused the NEW task but did not abort the in-flight one.
```

The three checks:

1. `t2` raised `CharterRevokedError` (asserted by the script).
2. `t3` printed `ok: task task_001 completed`. The cached verdict survived
   the revocation.
3. The scratch data dir under `data/cookbook_03/` contains a Charter whose
   `lifecycle.status == "revoked"`.

## Common pitfalls

- **Caching the Charter object instead of the verdict.** A cached Charter
  is just a snapshot — your re-grading code will *still* fetch the new
  Charter on the next loop iteration. The cache key needs to be the
  *verdict* keyed by *task_id*, not the Charter.
- **Per-principal cache instead of per-task.** A cache keyed by
  `(principal_id, agent_id)` is shared across tasks. Now a new task can
  inherit the cached verdict of an old task and ride past a revocation.
  Always key the cache on the task identity.
- **Forgetting to drop the ticket.** A long-lived ticket cache is a
  capability leak: even if the Charter is revoked and the task completed
  years ago, the ticket *could* still be honored. Drop tickets on task
  completion, AND set a sanity TTL on the cache so an abandoned ticket
  cannot live forever.
- **"Strict mode" callers re-fetching too aggressively.** The basic pattern
  trusts the ticket until task completion. For tasks that include a
  destructive irreversible step (a wire transfer, a `DROP TABLE`), you
  *should* re-fetch the Charter immediately before that one step, even
  though it costs an extra HTTP round-trip. The right framing: "trust the
  ticket for the work you can roll back, re-verify for the work you
  can't." This is the same trade-off Web Bot Auth surfaces between
  per-session and per-request signing.
- **Trying to set a "grace period" with a timer.** A wall-clock grace
  period ("honor the Charter for 5 more minutes after revocation") looks
  attractive but is impossible to reason about under concurrency: how do
  you handle a task that was queued at minute 4 but hasn't started yet?
  The ticket pattern sidesteps the question — a task either has a ticket
  (it started before the revocation) or it doesn't (it started after).

## Related guides

- [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  — issue the Charter you're about to revoke.
- [05 — Integrate OpenAI Agents SDK](05-integrate-openai-agents-sdk.md) —
  the `@charter_gated` decorator runs `charter_preflight` *every* call by
  default; pair it with the ticket pattern in your tool body for the
  strict path.
- [10 — Audit the transparency log](10-audit-transparency-log.md) — every
  revocation re-signs the Charter, and the re-sign appends to the
  transparency log. The audit trail shows the revocation timestamp
  cryptographically.

## What's next

The protocol-layer side of revocation is in
[`charter/cli.py`](../../charter/cli.py)'s `revoke` command. The fetch-side
detection is in `_fetch_and_verify` in
[`charter/mcp_server.py`](../../charter/mcp_server.py) — that's where
`CharterRevokedError` is raised. Read both functions together; the in-flight
pattern lives entirely *outside* of them, in the calling agent's tracking
code.
