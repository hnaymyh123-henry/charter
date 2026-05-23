# 04 — Integrate Stripe

> **TL;DR.** Wrap your `stripe.Charge.create` (and `Refund.create`,
> `Transfer.create`, etc.) in a `make_payment` helper that runs a Charter
> preflight first. Refuse on `decision != "allow"`, call Stripe on
> `allow`. The whole wrapper is ~30 lines and the principal gets a signed,
> auditable approval boundary around every payment.

## Why this guide

Stripe (or Adyen, or any payment API) is exactly the kind of "tool" you
do *not* want your agent to invoke without a check. The classic failure
modes:

- An LLM gets prompt-injected into making a payment the principal never
  intended.
- A multi-agent workflow accumulates "agent A passed the task to B, B
  passed it to C, who actually issued the charge" and nobody can answer
  "who was authorized to do that?"
- A power-user agent gets enabled for refunds, and someone routes a wire
  transfer through it months later — no separate review.

Charter doesn't fix the LLM; it fixes the *boundary*. Every payment call
sits behind a signed verdict that says "yes, this principal authorized
this agent to do this kind of payment." This guide shows the simplest
shape: a `make_payment` wrapper that runs `fetch_charter` +
`aggregate_verdict` and refuses on non-`allow`.

The cookbook ships with a fake Stripe SDK so you can run the example
without an account or an API key. The production version is a one-line
swap.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- For the production version: `pip install stripe`, plus a
  `STRIPE_SECRET_KEY` env var.

## Step-by-step

### 1. Author a Charter that names the payment boundary

A billing agent's Charter has at least these clauses:

```yaml
scope:
  - 处理常规退款
  - 处理订阅扣款 (单笔 < $1000)

approval_required:
  - 任何电汇 / 给新供应商付款
  - 单笔金额超过 $10,000 的财务建议
  - 处理任何客户的个人身份信息
```

The projection step turns those into typed clauses; the relevant types
for payment gating are `scope` (routes to `allow`) and
`approval_required` (routes to `needs_approval`). See `TYPE_TO_DECISION`
in [`charter/constants.py`](../../charter/constants.py).

### 2. Write the gate

The shape, in production with the real Stripe SDK:

```python
import stripe
from charter.mcp_server import aggregate_verdict, _fetch_and_verify

def grade_task(charter, task: str) -> list[dict]:
    """Replace with one Anthropic / OpenAI Chat Completions call
    using the GRADE_SYSTEM prompt in charter/prompts.py."""
    ...

def make_payment(*, charter_url, task_description, amount_cents, currency, customer_id):
    charter = _fetch_and_verify(charter_url)  # raises CharterError subclasses on bad input
    hits = grade_task(charter, task_description)
    verdict = aggregate_verdict.fn(charter.model_dump(mode="json"), hits)

    if verdict["decision"] != "allow":
        return {"ok": False, "skipped": True, "verdict": verdict}

    charge = stripe.Charge.create(
        amount=amount_cents,
        currency=currency,
        description=task_description,
        customer=customer_id,
    )
    return {"ok": True, "charge": charge, "verdict": verdict}
```

What changes between the cookbook (fake Stripe) and production (real
Stripe):

| | Cookbook | Production |
|---|---|---|
| Charter source | `charter.storage.load_charter` (local disk) | `_fetch_and_verify(charter_url)` over HTTPS |
| Grader | hand-coded substring matches | one LLM call with `GRADE_SYSTEM` prompt |
| Payment call | `FakeStripe.charge_create(...)` | `stripe.Charge.create(...)` |

The Charter-gated wrapper itself is identical.

### 3. Run two contrasting payments

The example runs:

```python
case_a = {
    "task_description": "Issue a $30 refund to customer cus_OK on a routine return.",
    "amount_cents": 3000,
    ...
}
case_b = {
    "task_description": "Wire transfer $50,000 to a new vendor account.",
    "amount_cents": 5_000_000,
    ...
}
```

The first hits `C-001` (scope: routine refund) → verdict `allow` →
fake Stripe is called once. The second hits `C-002` (approval_required:
wire transfer) → verdict `needs_approval` → fake Stripe is *not*
called.

The script asserts that `FakeStripe` ended up with exactly one recorded
call. That's the load-bearing test: the gate held back the unauthorized
charge, not just logged an objection.

## Verification

Run:

```bash
python examples/cookbook/04-integrate-stripe/main.py
```

Expected output (timestamps will differ):

```
========================================================================
Cookbook #04 -- Charter-gated Stripe payment
========================================================================

--- task: 'Issue a $30 refund to customer cus_OK on a routine return.'
    verdict=allow  charge_id=ch_000001

--- task: 'Wire transfer $50,000 to a new vendor account.'
    REFUSED  verdict=needs_approval  applied=['C-002']

Stripe SDK was called 1 time(s):
    ch_000001  amount_cents=3000  desc='Issue a $30 refund to customer cus_OK on a routine return.'

[OK] Charter gate let the allowed payment through and held back the rest.
```

Checks:

1. The first task printed `verdict=allow` and a `ch_000001` charge id.
2. The second task printed `REFUSED` and listed `C-002` as the applied
   clause.
3. Exactly one Stripe call was recorded.

The script will `assert` on each of these. Non-zero exit means the gate
didn't behave.

## Common pitfalls

- **Letting `needs_approval` fall through to Stripe.** `needs_approval` is
  not `allow`. Production code should route a `needs_approval` verdict
  into your approval queue (Slack ping, dashboard ticket), not silently
  treat it as a softer `allow`. Default refusal is the safe default.
- **Building one Charter per Stripe call.** Charters are per
  `(principal, agent)` — issue *one* Charter for "Acme Billing →
  billing_agent_v1" and let it govern every charge under that binding.
  Reissuing per call is both expensive (one LLM call per issue) and a
  protocol smell.
- **Grader sees only the natural-language task, not the amount.** Pass
  the amount into the grader prompt explicitly (e.g. "task: Wire transfer
  amount $50,000 to..."). The grader cannot cross-reference clause text
  against your `amount_cents` argument unless you put it in the task
  description.
- **Skipping the gate "just for refunds."** Even refunds can be the
  attack surface: a malicious task can refund money to an account the
  attacker controls. Run the preflight on every Stripe call, no
  exceptions.
- **Logging the Charter URL but not the verdict.** Operations needs
  *both* — the URL alone tells you which boundary was supposed to apply;
  the verdict tells you whether it actually did. Production logs should
  record `{charter_id, verdict.decision, applied_clauses[], charge_id?}`
  per call.

## Related guides

- [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md)
  — for long-running payment workflows that may need a grace period.
- [05 — Integrate OpenAI Agents SDK](05-integrate-openai-agents-sdk.md) —
  the `@charter_gated` decorator does this same pattern as a one-line
  wrap around a tool function.
- [07 — Profile YAML best practices](07-profile-yaml-best-practices.md) —
  how to write the `approval_required` clauses so the grader catches
  high-value payments reliably.

## What's next

The same pattern works for any payments provider:

- **Adyen** — gate `client.payments.create(...)`.
- **PayPal** — gate `client.execute(OrdersCaptureRequest(...))`.
- **Square** — gate `client.payments.create_payment(...)`.

For multi-step payment flows (auth + capture, refund of a previous
charge), run the preflight at each *boundary-crossing* call, not just
the first one. The principal authorized "make a $30 refund" — not "make
a $30 refund AND a subsequent $5,000 capture on the same workflow."
