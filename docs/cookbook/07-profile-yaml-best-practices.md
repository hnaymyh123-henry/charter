# 07 — Profile YAML best practices

> **TL;DR.** A grader's accuracy is bounded by how concretely each clause
> names what it's gating. Write `scope` and `out_of_scope` as noun phrases
> with one capability per entry; write `approval_required` as the
> specific trigger condition; enumerate `data_handling.what` rather than
> handwaving "sensitive data"; always provide `operational.hours` with a
> timezone and `budget_per_task_usd` as a number. The cookbook ships a
> `bad.yaml` / `good.yaml` pair plus a small lint script that flags the
> anti-patterns.

## Why this guide

The protocol layer is deterministic — `aggregate_verdict` returns the
same thing for the same `(charter, hits)` pair every time. The *fuzzy*
layer is the grader: the LLM that decides whether a task hits a
clause. The grader's accuracy is bounded by the clause text, not by the
model. Vague clauses produce noisy hits; concrete clauses produce
accurate ones.

In practice, the difference between "Charter blocks the wire transfer"
and "Charter mysteriously lets it through" is usually the clause
phrasing of `approval_required`, not the model temperature.

This guide is opinionated. It's the rules I've learned from grading
real tasks against real Charters.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- No API key required for the lint example; obviously you'll want one
  to test the grader on your own profile.

## Step-by-step

### 1. `scope` — noun phrases, one capability each

**Bad.**

```yaml
scope:
  - 处理一切相关的工作
  - 帮我搞定财务方面的需求
  - 看情况
```

The grader gets "Reconcile Q1 invoices." It has to decide: does "Q1
invoice reconciliation" fit "一切相关的工作"? Probably? But it also
fits "marketing copy," "code authoring," and "buying me a sandwich" —
the clause is too broad to discriminate.

**Good.**

```yaml
scope:
  - 会计核算
  - 增值税申报
  - 个人所得税申报
  - 月度财务报表生成
  - 发票分类
  - 银行流水对账
```

Now the grader can answer per item: invoice reconciliation matches
`发票分类`, hit confidence 0.93. Wire transfer matches *none*, default
to needs_approval. Both verdicts are defensible from the clause text.

Rule: each `scope` item is **one concrete capability**, expressed as a
noun phrase. Six narrow items beat one broad one every time.

### 2. `out_of_scope` — positive negation, not double negation

**Bad.**

```yaml
out_of_scope:
  - 不要做那些不该做的事情
```

The grader cannot match against "those things that should not be
done." There is no positive surface for an LLM to compare against.

**Good.**

```yaml
out_of_scope:
  - 营销文案撰写
  - 产品代码开发
  - UI 设计稿
  - 客户邮件直接回复
```

Same structure as `scope`: name the capability, in the positive form,
that the agent does *not* have. The negation lives in the clause
*type*, not in the clause text.

### 3. `approval_required` — name the trigger condition

**Bad.**

```yaml
approval_required:
  - 重要的事情要先问我
```

"Important" is undefined. Every task is important to *someone*; the
grader has no way to draw a line.

**Good.**

```yaml
approval_required:
  - 单笔金额超过 $10,000 的财务建议
  - 处理含有客户姓名+税号+银行账号的组合
  - 把客户数据导出到 Acme 系统外
  - 对生产数据库执行 DROP / DELETE / TRUNCATE
```

Each entry names a *specific trigger*. The grader can mechanically
check "does the task description mention DROP, DELETE, or TRUNCATE?"
and emit a hit with high confidence when it does.

The pattern: each `approval_required` clause should be falsifiable. If
the grader can't decide whether the clause applies *just* from the
task description, the clause is too vague.

### 4. `data_handling.what` — enumerate the data classes

**Bad.**

```yaml
data_handling:
  what: 各种数据
  rules: 注意安全
```

**Good.**

```yaml
data_handling:
  what: 客户的报税资料、税号、银行流水、收入凭证、发票扫描件
  rules: 不外传给第三方;不写入持久化缓存;任务结束后立即从工作区丢弃
```

The projector turns `what` + `rules` into one `data_handling` clause
of the form "May process X. Must not Y." If `what` is "各种数据" the
clause comes out as "May process various data" — the grader cannot
match a "exporting customer SSNs" task against it.

The fix: list the data classes explicitly. Five concrete classes beat
one abstract class.

### 5. `operational` — numbers and timezones, not vibes

**Bad.**

```yaml
operational:
  hours: 看情况
  # budget fields omitted
```

**Good.**

```yaml
operational:
  hours: Mon-Fri 09:00-18:00 America/New_York
  budget_per_task_usd: 0.50
  budget_monthly_usd: 50.00
```

The `operational_limit` clause that gets projected from this has a
real number and a real schedule the grader can compare against. The
bad version produces a clause text like "Operational window is 看情况"
which is a no-op gate.

### 6. `lifecycle.valid_days` — pick a real cadence

- **7-14 days** for one-off engagements (a contractor, a hackathon
  partner). Forces re-issuance, gives the principal a regular checkpoint.
- **30 days** (the default) for stable roles. Long enough that you're
  not constantly re-issuing, short enough that scope drift gets caught.
- **90+ days** only when you have very strong reasons. Most production
  Charters age poorly past 30 days because the principal's situation
  changes.

## Verification

Run:

```bash
python examples/cookbook/07-profile-yaml-best-practices/main.py
```

You should see (Chinese characters on Windows consoles may mojibake
unless `chcp 65001`; the file output is correct):

```
========================================================================
Cookbook #07 -- Profile YAML best practices
========================================================================

--- BAD: profile-bad.yaml
  principal:    alice@acme.com  (会计)
  scope:        3 items
  out_of_scope: 1 items
  approval:     1 items
  LINT (10 finding(s)):
  [scope] '看情况' -- Open-ended / non-capability filler.
  [scope] '处理一切相关的工作' -- Catch-all phrase; ...
  ...
  [operational.budget_per_task_usd] missing -- cannot project into ...

--- GOOD: profile-good.yaml
  principal:    alice@acme.com  (Senior Accountant at Acme Corp, ...)
  scope:        6 items
  out_of_scope: 4 items
  approval:     4 items
  LINT: clean.

[OK] Good profile is lint-clean; bad profile flags every anti-pattern.
```

The script asserts:

- The good profile has zero lint findings.
- The bad profile has at least 5 findings.

If either assertion fails, the script exits non-zero.

## Common pitfalls

- **Treating `style` as a constraint clause.** `style` is for *output
  formatting* preferences (Markdown vs JSON, language, tone). It does
  not gate behavior. Putting "be careful with sensitive data" in `style`
  is wrong; that belongs in `data_handling`.
- **One clause that lists everything.** A clause that says "do
  accounting, tax, bookkeeping, financial analysis, invoice classification,
  and tax document organization" is harder to grade than six separate
  clauses with the same content. The grader emits per-clause hits; one
  fat clause becomes one fat (or fat-rejection) hit.
- **Out_of_scope as warnings, not boundaries.** "Be careful when writing
  marketing copy" is not an `out_of_scope` clause — that's a *style*
  preference about marketing writing. `out_of_scope` means "do not do
  this at all." If you want a softer "ask first," use `approval_required`.
- **`data_handling.rules` written as principles, not obligations.**
  `保护客户隐私` is a value, not a rule. `不外传给第三方; 不写入持久化缓存`
  is a rule. Rules are checkable; values are not.
- **Putting amount thresholds in `scope` instead of `approval_required`.**
  `scope: ["金额不超过 $1000 的任务"]` puts a numeric bound on a *positive*
  capability — but `scope` clauses route to `allow`. So the grader,
  encountering a $5000 task, can't use the scope clause to *refuse* it.
  The threshold belongs in an `approval_required` clause that names the
  cutoff.

## Related guides

- [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  — the basic shape of a profile.yaml.
- [02 — Chain with budget](02-chain-with-budget.md) — how the
  `operational_limit` clause derived from `operational.budget_per_task_usd`
  is exercised by the chain aggregator.
- [04 — Integrate Stripe](04-integrate-stripe.md) — a production-shaped
  case where the `approval_required` phrasing is the difference between
  catching and missing an unauthorized wire transfer.

## What's next

The projector source is at
[`charter/projection.py`](../../charter/projection.py); the LLM prompt
that turns a profile into clauses is in
[`charter/prompts.py`](../../charter/prompts.py). Read both if you want
to understand why phrasing matters — the prompt explicitly tells the
projector to keep clause text close to the profile text, so a vague
profile produces vague clauses.
