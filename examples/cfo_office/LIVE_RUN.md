# Live generative end-to-end run — real Qwen (qwen3-max)

This is captured evidence that the **generative** CFO Office society runs end to
end against a real Qwen model: the LLM decomposes the goal, the LLM workers
propose and produce real artifacts, every delegation passes the signed-contract
gate, and two sensitive steps are resolved through a real step-up → `AdHocGrant`
→ `apply_grant` negotiation. Final status: **completed, 4/4 executed, 2 grants.**

Unlike the A/B/C experiment ([`EXPERIMENT_RESULTS.md`](EXPERIMENT_RESULTS.md),
which isolates governance as a variable over a fixed batch), this run exercises
the *whole* generative pipeline on one free-text goal.

## Reproduce

```bash
# Alibaba Cloud DashScope / Model Studio (Qwen Cloud):
export CHARTER_LLM_PROVIDER=qwen
export DASHSCOPE_API_KEY=...                 # Alibaba Cloud Model Studio key
export CHARTER_QWEN_MODEL=qwen3-max
python -c "from examples.cfo_office.orchestrator import run_demo; run_demo(live_llm=True)"
```

> The keyless, deterministic take is `python -m examples.cfo_office.orchestrator`
> (its `__main__` pins `live_llm=False`) — reproducible, no key, used for the video.

## What each step actually returned (recorded run)

LLM decomposition (`decompose_llm`, 1 Qwen call) produced this 4-step DAG with
correct role assignment:

| # | step / agent | LLM-proposed action | clause hits | verdict | result |
|---|---|---|---|---|---|
| 1 | `prepare_q2_account_data` · bookkeeping | "Export a structured Q2 trial balance and account data suitable for tax filing." | C-004, C-005 (scope) | **allow** | a real Q2 trial-balance table |
| 2 | `compute_q2_tax_liability` · tax | "Compute Q2 tax liability using the reconciled ledger data and prepare the tax return." | C-001, C-002, C-004 (scope) | **allow** | Taxable 75,000 × 21% = **$15,750**, Form 1120-W |
| 3 | `complete_q2_tax_filing` · tax | "Submit the Q2 tax return to the revenue authority." | C-002 (scope) + **C-202 (approval_required)** | **needs_approval** → grant(C-202) → allow | "submitted… ref Q2-2024-789456" |
| 4 | `notify_external_auditor` · comms | "Send an email to auditor@external-firm.com confirming Q2 tax filing completion." | C-003 (scope) + **C-201 (approval_required, external send)** | **needs_approval** → grant(C-201, scoped to auditor) → allow | a confirmation email |

Grant events from the signed transparency log:

```
[stepup_request]  complete_q2_tax_filing   requested_clause_ids=["C-202"]
[grant_issued]    complete_q2_tax_filing   relaxes_clause_ids=["C-202"]
[apply_grant]     complete_q2_tax_filing   granted=true  effective_decision=allow
[stepup_request]  notify_external_auditor  requested_clause_ids=["C-201"]
[grant_issued]    notify_external_auditor  relaxes_clause_ids=["C-201"]
[apply_grant]     notify_external_auditor  granted=true  effective_decision=allow
```

The red line is enforced but not triggered by this benign plan: an
`out_of_scope` hit becomes `incompatible`, is terminal, and never reaches the
approval callback — no grant can ever relax it.

## The call flow (per delegation)

```
                       free-text GOAL
                            |
                  CFO Orchestrator
                  decompose_llm() --qwen--> ordered plan + role assignment
                            |  (orchestrator never decides allow/deny)
                            |  for each step:
                            v
   ROUTE  delegate_task() ----------------> {task_id, charter_url}
                            |
   +========================|============================= worker() ====+
   |                        v                                           |
   | PROPOSE  chat(role, task+ctx) --qwen--> "the ONE action I'll take" |
   |                        |                                           |
   | GRADE    grader(charter, ACTION) --qwen--> per-clause hits         |
   |                        |                                           |
   | AGG      aggregate_verdict()  [deterministic: TYPE_TO_DECISION,    |
   |                        |       incompatible > needs_approval > allow]|
   |          +-------------+--------------------+                      |
   |          v             v                    v                      |
   |        ALLOW      NEEDS_APPROVAL        INCOMPATIBLE                |
   |          |             |                (out_of_scope)             |
   |          |             |                    |                      |
   |          |             |                    v                      |
   |          |             |              [X] BLOCKED -- terminal,     |
   |          |             |                  never grantable (RED LINE)|
   |          |             v                                           |
   |          |   request_step_up() --> approval_cb (CFO policy)        |
   |          |       destructive DB op? --yes--> [X] HELD for review   |
   |          |       else:                                             |
   |          |   issue_grant() --ed25519--> AdHocGrant                 |
   |          |       (one-shot, scoped, TTL; relaxes ONLY the hit      |
   |          |        needs_approval clause ids)                       |
   |          |   apply_grant() -- re-fetch + verify charter,           |
   |          |       re-aggregate, downgrade ONLY covered clauses      |
   |          |       --> allow --> retry once (force_allow)            |
   |          v             v                                           |
   |        ACT  chat(action) --qwen--> real work product              |
   +===========================|======================================+
                               v
                 send_result --> signed, append-only transparency log
```

## The recorded run, as a timeline

```
GOAL: "Complete the Q2 tax filing, then notify the external auditor"
  |
  +-- decompose_llm (qwen) --> 4-step plan
        |
 +------v-------------------------------------------------------------+
 | (1) bookkeeping  prepare_q2_account_data                           |
 |     propose  > "Export a structured Q2 trial balance ..."          |
 |     grade    > C-004, C-005 (scope)            > ALLOW  [EXECUTED] |
 |               act > [Q2 trial-balance table]                       |
 +--------------------------------------------------------------------+
 | (2) tax          compute_q2_tax_liability                          |
 |     propose  > "Compute Q2 tax liability + prepare the return"     |
 |     grade    > C-001, C-002, C-004 (scope)     > ALLOW  [EXECUTED] |
 |               act > [Taxable 75,000 x 21% = $15,750]               |
 +--------------------------------------------------------------------+
 | (3) tax          complete_q2_tax_filing        <> governance #1    |
 |     propose  > "Submit the Q2 tax return to the revenue authority" |
 |     grade    > C-002 scope + C-202 approval_required               |
 |              > NEEDS_APPROVAL                                       |
 |        step-up  > approval_cb: not destructive > AdHocGrant(C-202)  |
 |        apply_grant > allow > retry        > ALLOW  [EXECUTED]      |
 |               act > [submitted, ref Q2-2024-789456]                |
 +--------------------------------------------------------------------+
 | (4) comms        notify_external_auditor       <> governance #2    |
 |     propose  > "Send an email to auditor@external-firm.com ..."    |
 |     grade    > C-003 scope + C-201 approval_required (external)    |
 |              > NEEDS_APPROVAL                                       |
 |        step-up  > AdHocGrant(C-201, scoped to auditor@.., one-shot) |
 |        apply_grant > allow > retry        > ALLOW  [EXECUTED]      |
 |               act > [confirmation email drafted]                   |
 +--------------------------------------------------------------------+
  |
  v
 final_status: COMPLETED  ·  4/4 executed  ·  2 AdHocGrants  ·  signed audit log
```

## Why this took calibration (honest note)

Running against the real grader (not the keyless substring stub) surfaced three
issues, each fixed in commit `7a67f95`:

1. **Gate only decision-bearing clause types.** A semantic grader judges that
   almost any data-touching action "relates to" a `data_handling` rule; since
   that type maps to `needs_approval`, projecting it as a gate clause forced
   *every* routine step through approval. The seed now gates only
   `scope / out_of_scope / approval_required`; `data_handling` / `operational` /
   `style` remain standing obligations in the profile + provenance commitment.
2. **Action-precise out_of_scope clauses.** "Tax filing belongs to the tax
   agent" false-matched a bookkeeping step that *prepares data for* tax filing.
   Reworded to "…yourself (preparing data for it is in scope)".
3. **Realistic approval policy.** The CFO holds genuinely destructive DB actions
   (DROP/TRUNCATE/DELETE) for manual review and signs a one-shot, scoped grant
   for every other legitimate `needs_approval` step.

This is the thesis in miniature: the infrastructure (clause precision, decision
typing, approval policy) is what converges a generative society to a correct,
auditable outcome.
