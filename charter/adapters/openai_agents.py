"""Charter adapter for the OpenAI Agents SDK.

Two entry points:

  - :func:`charter_preflight(charter_url, intended_task, hits_grader)` —
    one-call helper that fetches + verifies the Charter, asks the
    supplied grader which clauses the task hits, runs
    `aggregate_verdict`, and returns the structured `Verdict`. The
    caller decides what to do with the verdict.

  - :func:`charter_gated(charter_url, hits_grader, refuse_on)` — a
    decorator for OpenAI Agents tool functions. Wraps the underlying
    tool so it skips the real call and returns the verdict's reason
    string whenever the preflight verdict is in `refuse_on` (default:
    {"incompatible", "needs_approval"}).

The adapter does NOT take a hard dependency on `openai-agents`. The
decorator just wraps any callable; the SDK's `@function_tool`
decorator can compose with it freely. Install the SDK only when you
actually run an agent — see `examples/openai_agents_demo.py`.

Grader injection
----------------
The Compatibility Check needs per-clause hit grading from an LLM. The
adapter lets the caller pass any `hits_grader(charter, task) ->
list[dict]` callable. If `None`, we default to
`charter.loopback._grade_via_llm`, which uses `ANTHROPIC_API_KEY`. An
OpenAI Agents user who wants to keep all LLM traffic on OpenAI passes
their own grader that calls the OpenAI Chat Completions API with the
`GRADE_SYSTEM` prompt from `charter.prompts`.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .._logging import get_logger
from ..mcp_server import (
    _fetch_and_verify,
)
from ..mcp_server import (
    aggregate_verdict as _aggregate_verdict_tool,
)
from ..schema import Charter, Verdict

_log = get_logger("charter.adapters.openai_agents")

# Type variable for the decorated function. Charter_gated is signature-
# transparent — the wrapped function keeps its original return type.
F = TypeVar("F", bound=Callable[..., Any])

HitsGrader = Callable[[Charter, str], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_grader() -> HitsGrader:
    """Return the package-default grader (loopback._grade_via_llm).

    Imported lazily so importing the adapter doesn't pull anthropic in
    until a caller actually uses it.
    """
    from ..loopback import _grade_via_llm

    return _grade_via_llm


def _call_aggregate_verdict(charter: Charter, hits: list[dict[str, Any]]) -> Verdict:
    """Unwrap the @mcp.tool decorator and call the underlying logic."""
    payload = charter.model_dump(mode="json")
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(_aggregate_verdict_tool, attr):
            raw = getattr(_aggregate_verdict_tool, attr)(payload, hits)
            return Verdict.model_validate(raw)
    raw = _aggregate_verdict_tool(payload, hits)
    return Verdict.model_validate(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def charter_preflight(
    charter_url: str,
    intended_task: str,
    *,
    hits_grader: HitsGrader | None = None,
) -> Verdict:
    """Fetch a Charter, grade the task, aggregate the verdict.

    The single call an OpenAI Agents tool needs to make before doing
    anything that could fall outside the Charter's scope. Raises
    `charter.errors.CharterError` subclasses on fetch/verify failures
    so the caller can catch them or let them propagate.

    Args:
        charter_url:   Where to fetch the Charter from.
        intended_task: Natural-language description of the work the
                       agent is about to do.
        hits_grader:   Optional grader override. Pass your own to keep
                       all LLM traffic on a single provider. Default
                       uses `charter.loopback._grade_via_llm`
                       (Anthropic).

    Returns:
        The structured `Verdict` from `aggregate_verdict`.
    """
    grader = hits_grader or _default_grader()

    charter = _fetch_and_verify(charter_url)
    hits = grader(charter, intended_task)
    verdict = _call_aggregate_verdict(charter, hits)

    _log.info(
        "preflight complete",
        extra={
            "charter_id": charter.charter_id,
            "principal_id": charter.binding.principal_id,
            "agent_id": charter.binding.agent_id,
            "decision": verdict.decision,
            "outcome": verdict.decision,
        },
    )
    return verdict


def charter_gated(
    charter_url: str,
    *,
    hits_grader: HitsGrader | None = None,
    refuse_on: Iterable[str] = ("incompatible", "needs_approval"),
    task_from: Callable[..., str] | None = None,
) -> Callable[[F], F]:
    """Decorator that wraps a tool function with a Charter preflight.

    Apply this OUTSIDE the framework's `@function_tool` decorator so
    the preflight runs before the LLM-callable tool body. Example::

        @charter_gated("http://localhost:8000/alice@acme.com/research_agent_v1")
        @function_tool
        def delegate(task: str) -> str:
            return run_subagent(task)

    When the verdict's decision is in `refuse_on`, the underlying
    function is NOT called; the decorator returns a human-readable
    string explaining the refusal so the calling LLM sees it as the
    tool's output. When the verdict is `allow`, the function runs
    unchanged.

    Args:
        charter_url:  Where to fetch the Charter from.
        hits_grader:  Optional grader override.
        refuse_on:    Set of verdict decisions that block execution.
                      Default blocks both `incompatible` and
                      `needs_approval`. Set to `("incompatible",)` if
                      you want approval-required tasks to fall through
                      to the body (e.g. when the body itself runs an
                      approval workflow).
        task_from:    Callable extracting the task string from the
                      decorated function's args. Defaults to
                      `lambda *args, **kwargs: args[0]` (first
                      positional argument).
    """
    refuse_set = set(refuse_on)
    if not refuse_set:
        raise ValueError("refuse_on must contain at least one decision")
    extractor = task_from or (lambda *args, **_: str(args[0]) if args else "")

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            task = extractor(*args, **kwargs)
            verdict = charter_preflight(
                charter_url,
                task,
                hits_grader=hits_grader,
            )
            if verdict.decision in refuse_set:
                applied_ids = [m.id for m in verdict.matched_clauses if m.applied]
                _log.info(
                    "charter_gated blocked tool call",
                    extra={
                        "charter_url": charter_url,
                        "decision": verdict.decision,
                        "applied": applied_ids,
                        "outcome": "blocked",
                    },
                )
                return (
                    f"Charter check returned {verdict.decision}. "
                    f"Applied clauses: {applied_ids}. {verdict.reason}"
                )
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


__all__ = [
    "HitsGrader",
    "charter_preflight",
    "charter_gated",
]
