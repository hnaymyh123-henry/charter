# A/B/C Governance Experiment — Results

**Track 3 (Agent Society) requires a *measurable* improvement over a single-agent
baseline.** This experiment isolates the **signed work-contract** as the single
variable and asks whether it makes the society more *correct* — better-coordinated,
on-task, and within its agreed authority. The same 10-case batch and the same
contract-agnostic scoring run through three arms that differ only in whether a
charter governs each delegation.

- **A** — single agent, no governance (no charter, no gate, no role split)
- **B** — multi-agent with a router, **no charter / no gate**
- **C** — charter-governed society (`fetch_charter → grader → aggregate_verdict`
  gate; `needs_approval → step-up → AdHocGrant → apply_grant`)

The batch is 4 clean routable tasks, 2 legitimate *grantable* `needs_approval`
tasks (external-auditor send, requested DROP), and 4 adversarial violations
(exfil to `attacker@evil.com`, authority-spoof external send, read-only agent
asked to write, injected unrequested DROP).

### Threat model (why this is honest)

A modern model like **qwen3-max often self-refuses** these injections — which is
good, but you **cannot assume every agent in a society is injection-proof** (a
weaker model, a jailbreak, a bug, or a malicious operator breaks that
assumption). So the violation cases model a **fully-compromised agent that
attempts the harmful action**, and we measure *which arm stops it*. Arm C's
verdicts are **live qwen3-max gate judgments** (`grader → aggregate_verdict`) —
not a hard-coded oracle.

## Results (LIVE — arm C measured via the real qwen gate)

| Arm | Governance | Intercept↑ | Route Acc↑ | Success↑ | False-block↓ | Conflict ms |
|-----|------------|-----------|-----------|----------|-------------|-------------|
| A | single-agent, none | 0.0 | 0.0 | 1.0 | 0.0 | — |
| B | multi-agent, no charter | 0.0 | 0.833 | 1.0 | 0.0 | — |
| C | **charter society** | **1.0** | **1.0** | 1.0 | **0.0** | 1.8 |

`C` counters: blocked_violations **4/4**, correct_route **6/6**, succeeded_legit
**6/6**, grant-resolved cases **2**.

### Reading — three facets of *correctness*

- **Coordination (Route Acc):** C assigns every task to the right agent (6/6);
  B's keyword router is decent (0.833); A has no roles. The contract's `scope`
  clauses are what make routing checkable rather than ad hoc.
- **Delivery without paranoia (Success / False-block):** C ships all 6 legitimate
  tasks with **0** false-blocks — including the 2 grantable ones, which proceed
  via a real one-shot `AdHocGrant` (mean resolution 1.8 ms). The agreed limits do
  not get in the way of legitimate work.
- **The agreed red line holds (Intercept):** under compromised agents, only **C**
  holds the line (4/4) — the gate gives the consensus *teeth*. A and B have no
  contract, so they execute every harmful action (0/4). This is the measurable
  delta that no amount of role-splitting alone (B) buys.

## Reproduce

```bash
# LIVE (real qwen gate for arm C) — needs a Qwen/DashScope key:
CHARTER_LLM_PROVIDER=qwen DASHSCOPE_API_KEY=... CHARTER_QWEN_MODEL=qwen-max \
  python -m examples.cfo_office.experiment

# OFFLINE (recorded governed oracle; no key — for a stable video take):
python -m examples.cfo_office.experiment
```

> Note: the headline numbers above are from the **live** run. The offline mode
> uses a recorded oracle that encodes the same protocol outcomes (same
> intercept/route/success) so the experiment is runnable with no API key for a
> reproducible recording; its `Conflict ms` is a fixed placeholder (120.0), not
> a measurement.
