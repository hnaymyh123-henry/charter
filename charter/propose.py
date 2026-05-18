"""Scope-rewrite engine for the `propose_within_scope` MCP tool.

Given a Charter + an `intended_task` + the `failed_verdict` that judged the
task `incompatible`, calls Claude once to produce a nearby in-scope rewrite
(or `None` if no viable rewrite exists).

This module is the LLM-touching half of the protocol's "delegation router"
behavior — it turns Charter from a refusal list into something that
actually moves delegation forward. The MCP tool wrapper lives in
`charter/mcp_server.py`; this module is the unit-testable helper.

Design notes:
  - Single-shot. The full design includes loopback verification + retry
    with prompt evolution and temperature annealing; that lives in
    `charter/loopback.py` (next iteration) and wraps this function.
  - LLM-stateless. The function reads `ANTHROPIC_API_KEY` from env and
    creates a fresh `anthropic.Anthropic` client per call, mirroring
    `charter/projection.py`. Tests monkeypatch `charter.propose.anthropic`.
  - Returns `None` when the model declines to propose a rewrite. The MCP
    wrapper translates that into `{rewrite_available: false, reason: ...}`.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from .constants import DEFAULT_MODEL
from .prompts import PROPOSE_SYSTEM
from .schema import Charter, RewriteProposal, Verdict


def _strip_markdown_fences(text: str) -> str:
    """Strip ```json``` / ``` fences if the model included them."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :].lstrip()
    return text


def propose_within_scope_llm(
    charter: Charter,
    intended_task: str,
    failed_verdict: Verdict,
    *,
    temperature: float = 0.2,
    extra_user_context: str | None = None,
) -> RewriteProposal | None:
    """Call Claude once to produce a `RewriteProposal` (or None).

    Args:
        charter:            The signed Charter, already fetched and verified.
        intended_task:      The original task that was judged `incompatible`.
        failed_verdict:     The verdict that flagged it; the model uses
                            `matched_clauses` to know what to avoid.
        temperature:        Sampling temperature. Defaults to 0.2 so the
                            single-shot path is mostly deterministic; the
                            loopback wrapper anneals upward across retries.
        extra_user_context: Optional extra string appended to the user
                            message. The loopback wrapper uses this to feed
                            back failures from earlier attempts.

    Returns:
        A `RewriteProposal` if the model produced a valid one, or `None` if
        the model returned literal `null` (no viable rewrite) or its output
        could not be parsed.

    Raises:
        RuntimeError if `ANTHROPIC_API_KEY` is not set. The MCP wrapper is
        responsible for catching this and returning a degraded response.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; propose_within_scope cannot call the LLM."
        )

    client = anthropic.Anthropic()
    model = os.environ.get("CHARTER_MODEL", DEFAULT_MODEL)

    charter_json = charter.model_dump_json(indent=2)
    verdict_json = failed_verdict.model_dump_json(indent=2)

    user_content = (
        f"Charter:\n```json\n{charter_json}\n```\n\n"
        f"intended_task:\n{intended_task}\n\n"
        f"failed_verdict:\n```json\n{verdict_json}\n```"
    )
    if extra_user_context:
        user_content += f"\n\n{extra_user_context}"

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        temperature=temperature,
        system=PROPOSE_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )

    text = _strip_markdown_fences("".join(b.text for b in message.content if b.type == "text"))

    if not text or text == "null":
        return None

    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None

    if data is None:
        return None

    try:
        return RewriteProposal.model_validate(data)
    except Exception:
        return None
