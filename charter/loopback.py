"""Loopback verification for the scope-rewrite engine.

The single-shot `propose_within_scope_llm` can produce rewrites that still
hit out_of_scope or approval_required clauses; the model follows the
prompt's hard rules but isn't perfect. Loopback wraps it: each rewrite
attempt is graded against the Charter, and if the rewrite still doesn't
verify as `allow`, we anneal temperature, feed the failure reason back
into the prompt, and try again.

Design notes:

  - This module is the ONLY place inside the charter package that makes
    multiple LLM calls per protocol step. Each attempt costs up to
    `2 × LLM call` (one to propose, one to grade). External calling
    agents that want minimal server-side LLM cost should use the
    single-shot `propose_within_scope` MCP tool directly and run their
    own grading loop.

  - Temperature annealing: 0.2 → 0.5 → 0.8 across attempts 1..N. Low T
    on the first try favors a deterministic best-guess; higher T on
    retries explores more of the rewrite space when the conservative
    option has failed.

  - Feedback loop: when attempt K fails, attempt K+1 receives an
    extra_user_context block listing which clauses the previous rewrite
    still hit and why. This is much more effective than just sampling
    again at higher temperature.

  - Termination: returns the first `RewriteProposal` whose graded
    verdict is `allow`; otherwise returns a `RewriteFailure` carrying
    the full attempt history.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from .constants import DEFAULT_MODEL
from .mcp_server import aggregate_verdict as _aggregate_verdict_tool
from .prompts import GRADE_SYSTEM
from .propose import _strip_markdown_fences, propose_within_scope_llm
from .schema import (
    Charter,
    RewriteAttempt,
    RewriteFailure,
    RewriteProposal,
    Verdict,
)

# Temperature schedule across retry attempts. Cap at the last value if
# max_attempts > len(_TEMPERATURES).
_TEMPERATURES: list[float] = [0.2, 0.5, 0.8]


def _call_aggregate_verdict(charter: Charter, hits: list[dict[str, Any]]) -> Verdict:
    """Invoke the FastMCP-decorated aggregate_verdict function directly.

    FastMCP wraps the underlying callable; we unwrap it the same way the
    test helpers do.
    """
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(_aggregate_verdict_tool, attr):
            raw = getattr(_aggregate_verdict_tool, attr)(charter.model_dump(mode="json"), hits)
            return Verdict.model_validate(raw)
    # Fallback for older FastMCP versions where the decorator is transparent.
    raw = _aggregate_verdict_tool(charter.model_dump(mode="json"), hits)
    return Verdict.model_validate(raw)


def _grade_via_llm(charter: Charter, intended_task: str) -> list[dict[str, Any]]:
    """Ask Claude which clauses the given task hits, returning the `hits`
    list `aggregate_verdict` expects.

    On any parse failure we return `[]`; aggregate_verdict then falls
    back to its zero-match default (`needs_approval`), which makes the
    loopback continue retrying rather than declaring success on a bad
    parse.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set; loopback grading cannot call the LLM.")

    client = anthropic.Anthropic()
    model = os.environ.get("CHARTER_MODEL", DEFAULT_MODEL)
    charter_json = charter.model_dump_json(indent=2)

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        temperature=0.0,
        system=GRADE_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Charter:\n```json\n{charter_json}\n```\n\nintended_task:\n{intended_task}"
                ),
            }
        ],
    )

    text = _strip_markdown_fences("".join(b.text for b in message.content if b.type == "text"))
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    hits = data.get("hits", [])
    return hits if isinstance(hits, list) else []


def _build_feedback(history: list[RewriteAttempt]) -> str | None:
    """Compose an `extra_user_context` block describing earlier failures.

    Returns `None` for the first attempt (no history yet).
    """
    if not history:
        return None

    lines = ["Earlier attempts were judged out-of-scope. Avoid repeating the same mistakes:"]
    for h in history:
        if h.proposal is None:
            lines.append(
                f"  - Attempt {h.attempt} (T={h.temperature}): no rewrite produced"
                f" ({h.failure_reason or 'unknown'})."
            )
            continue
        applied_ids = [m.id for m in (h.verdict.matched_clauses if h.verdict else []) if m.applied]
        applied_str = ", ".join(applied_ids) if applied_ids else "(none)"
        lines.append(
            f"  - Attempt {h.attempt} (T={h.temperature}): rewrite"
            f" {h.proposal.rewritten_task!r} still hit clauses: {applied_str}."
        )
    lines.append(
        "Produce a different rewrite that avoids these applied clauses while"
        " still fitting an existing scope clause."
    )
    return "\n".join(lines)


def propose_within_scope_verified(
    charter: Charter,
    intended_task: str,
    failed_verdict: Verdict,
    *,
    max_attempts: int = 3,
) -> RewriteProposal | RewriteFailure:
    """Generate a rewrite that has been graded as `allow` against the Charter.

    Up to `max_attempts` rewrite-then-grade iterations. Returns the first
    `RewriteProposal` that grades as `allow`, or a `RewriteFailure` with
    the full attempt history on exhaustion.

    Args:
        charter:        The signed, verified Charter.
        intended_task:  The original task that failed compatibility.
        failed_verdict: The verdict that flagged it; passed into the
                        first rewrite prompt.
        max_attempts:   Maximum rewrite attempts. Default 3.

    Raises:
        RuntimeError if `ANTHROPIC_API_KEY` is unset. The MCP wrapper
        catches this.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    history: list[RewriteAttempt] = []

    for i in range(max_attempts):
        attempt_num = i + 1
        temp = _TEMPERATURES[min(i, len(_TEMPERATURES) - 1)]
        feedback = _build_feedback(history)

        proposal = propose_within_scope_llm(
            charter,
            intended_task,
            failed_verdict,
            temperature=temp,
            extra_user_context=feedback,
        )

        if proposal is None:
            history.append(
                RewriteAttempt(
                    attempt=attempt_num,
                    temperature=temp,
                    proposal=None,
                    verdict=None,
                    failure_reason="LLM returned null or unparseable output",
                )
            )
            continue

        # Grade the rewrite against the same Charter.
        hits = _grade_via_llm(charter, proposal.rewritten_task)
        verdict = _call_aggregate_verdict(charter, hits)

        history.append(
            RewriteAttempt(
                attempt=attempt_num,
                temperature=temp,
                proposal=proposal,
                verdict=verdict,
            )
        )

        if verdict.decision == "allow":
            return proposal

    return RewriteFailure(
        attempts=history,
        reason=(
            f"Exhausted {max_attempts} attempts without producing an"
            " in-scope rewrite that graded as `allow`."
        ),
    )
