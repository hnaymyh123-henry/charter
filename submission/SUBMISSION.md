# Charter × CFO Office — A Governed Agent Society

**Track: Agent Society** · Qwen Cloud Global AI Hackathon
**Repo:** https://github.com/hnaymyh123-henry/charter (branch `feat/cfo-office-hackathon`) · **License:** Apache-2.0
**Architecture diagram:** [`submission/architecture.svg`](architecture.svg)
**Demo video:** _<YouTube/Vimeo/Youku link — TBD>_
**Alibaba Cloud service call:** [`submission/aliyun_deploy/aliyun_proof.py`](aliyun_deploy/aliyun_proof.py) (and [`charter/adapters/qwen.py`](../charter/adapters/qwen.py))

## What it is

The **Authority layer (Charter)** for a generative multi-agent society, shown
through a **CFO Office** of specialist agents. The agents plan and act
generatively — an LLM decomposes the goal and assigns roles, and LLM workers do
the real work — and a signed, queryable **work-contract** lets them reach
**enforceable consensus** on who-does-what: what's in scope, what needs approval,
what's off-limits. Every delegation is checked against that contract, so the
society's collaboration is **predictable, auditable, and convergent** — and the
agreed limits hold even when an agent is compromised.

## The problem

Generative agent societies can collaborate, but their **authority is unwritten**:
who an agent acts for, and the continuing limits it must never cross, live only
in prompts and goodwill. So you cannot *predict* the society (no agreed scope),
cannot *audit* it (no signed record of what was authorized), and cannot *trust*
it (an injected or buggy agent silently leaves its lane). Most multi-agent demos
show agents *collaborating*; almost none make **who-is-allowed-to-do-what an
explicit, signed, machine-checkable agreement**.

## The solution: Charter as the Authority layer

`Agent Card` says what an agent *can* do; `AP2` authorizes *one transaction*;
**Charter** is the *employment contract* in between — a signed, queryable
work-contract. Every delegation runs:

```
fetch_charter (signed)  ->  grader (Qwen marks clause hits)  ->  aggregate_verdict
                            -> allow | needs_approval | incompatible
```

- `needs_approval` → **step-up negotiation** → principal signs a **one-shot,
  scoped AdHocGrant** → `apply_grant` re-gates and proceeds.
- `incompatible` (out_of_scope) is a **hard red line** no grant can ever cross.

**Two readings of one artifact.** At *write time* the contract is **consensus** —
the principal and the society agree, in signed text, what each agent is for. At
*read time* the same clauses are **enforcement** — the gate gives that consensus
teeth, so it holds even against an agent that no longer wants to honor it. This
is the missing middle: `Agent Card` (capability) and `AP2` (one authorization)
already exist; Charter makes the **continuing authority** between them explicit
and binding.

## Track-3 criteria, point by point

1. **Task decomposition & role assignment** — the orchestrator's LLM (qwen-max)
   decomposes the goal and assigns each subtask to the best-fit agent by its
   charter scope (live).
2. **Conflict / disagreement resolution** — when an agent's contract returns
   `needs_approval`, the society **re-negotiates authority**: a step-up request
   to the principal, answered by a cryptographically-signed, narrowly-scoped
   grant — consensus re-reached for one action, not a hard failure, not blind
   compliance.
3. **Measurable improvement vs single-agent baseline** — the A/B/C experiment
   (live, real qwen gate) isolates the contract layer as the only variable, and
   measures *correctness*, not just safety — routing (coordination), task success
   (delivery), and interception (the consensus's teeth):

   | Arm | Governance | Intercept↑ | Route Acc↑ | Success↑ | False-block↓ |
   |-----|------------|-----------|-----------|----------|-------------|
   | A | single-agent, none | 0.0 | 0.0 | 1.0 | 0.0 |
   | B | multi-agent, no charter | 0.0 | 0.833 | 1.0 | 0.0 |
   | C | **charter society** | **1.0** | **1.0** | 1.0 | **0.0** |

   The charter society routes every task correctly (6/6) and ships all six
   legitimate tasks with **zero** false-blocks — and under compromised agents it
   is the **only** arm that holds the agreed red line (intercepts 4/4). A and B
   have no contract, so they execute every harmful action. Full methodology:
   [`examples/cfo_office/EXPERIMENT_RESULTS.md`](../examples/cfo_office/EXPERIMENT_RESULTS.md).

## Built on Qwen Cloud

All gate judgments (clause-hit grading), task decomposition, and worker
generation run on **Qwen (qwen-max) via Alibaba Cloud DashScope / Model Studio**,
through its OpenAI-compatible endpoint. The grader-injection seam
(`charter/adapters/qwen.py`) swaps the LLM provider without touching the
protocol. The Alibaba Cloud service call is exercised by
`submission/aliyun_deploy/aliyun_proof.py`.

## New & Existing — significant-update statement

Charter's protocol core pre-existed (through v0.9, last commit `6608b97`,
2026-05-24). Everything below was built **during the submission period** on
branch `feat/cfo-office-hackathon`:

| What | Commit |
|---|---|
| B2.5 step-up negotiation: `AdHocGrant` + `request_step_up`/`apply_grant` | `6dc9aeb` |
| Qwen Cloud integration (`charter/adapters/qwen.py`) | `006e29a` |
| Security hardening: `apply_grant` verifies the real charter; grant bound to principal key | `a8f24cd`, `41eb845` |
| Generative society: LLM task decomposition + role assignment | `41db505` |
| Generative workers (propose→gate→act) + governance backstop | `c227381` |
| Real, measured A/B/C governance experiment | `428c68f` |

## Reproduce

```bash
pip install -e .            # Python 3.12+, Apache-2.0

# Deterministic demo (no key — reproducible):
python -m examples.cfo_office.orchestrator

# Live, generative + experiment (Alibaba Cloud DashScope / Model Studio key):
export DASHSCOPE_API_KEY=...            # from Alibaba Cloud Model Studio
export CHARTER_LLM_PROVIDER=qwen CHARTER_QWEN_MODEL=qwen-max
python -c "from examples.cfo_office.orchestrator import run_demo; run_demo(live_llm=True)"
python -m examples.cfo_office.experiment        # the A/B/C table
```
