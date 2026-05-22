"""Charter adapter for the AP2 (Agent Payments Protocol) Mandate envelope.

This adapter wires Charter's *continuing-authority* check into AP2's
*per-transaction-authorization* envelope, so a payment-side verifier can
ask both questions in one call and refuse the moment either layer says
no.

AP2 Mandate shape (assumed, dict-typed)
---------------------------------------

`mandate` is treated as a plain dict — the AP2 Python SDK is not yet
stable in 2026, and pinning a schema would lock us out of every wire
revision until it lands. The fields this adapter touches:

    {
      "payer":     "...",         # opaque payer identifier
      "payee":     "...",         # opaque payee identifier
      "amount":    {"value": float, "currency": "USD"},
      "task":      "natural-language description of the work",
      "signature": "...",          # arbitrary signature blob; non-empty == valid
      "extensions": {              # free-form key/value map
        "charter_url": "https://issuer/.../charter"
      }
    }

The `signature` field is intentionally NOT cryptographically verified
here — AP2 owns its own signature scheme, and this adapter is the entry
point a host (PSP / wallet / pay-by-agent gateway) calls AFTER it has
already validated the mandate envelope. The mock check in
:func:`_check_mandate` is a placeholder for the real AP2 verifier hook.
See the AP2 spec (https://google.github.io/agent-payments-protocol/ —
URL placeholder, the wire spec is still draft) for the production
signature path.

Why charter_url goes under `extensions`
---------------------------------------

AP2's `extensions` map is the documented extension point for
non-payment metadata that the mandate carries opaquely between issuer,
mandate verifier, and the resource side. Putting `charter_url` here
keeps Charter compatible with mandates produced by hosts that have no
Charter awareness — they pass `extensions` through untouched, and a
Charter-aware verifier picks it up.

Public surface
--------------

  - :func:`embed_charter_in_mandate` — issuer-side helper that writes
    `extensions.charter_url` without mutating any other field.
  - :func:`verify` — verifier-side entry point that runs both the
    AP2 mandate check and the Charter compatibility check and
    collapses them into one :class:`AP2VerifyResult`.

Dependency injection
--------------------

`verify` takes `fetch_charter_fn` so tests (and hosts that prefer their
own Charter cache layer) can plug in a mock or a different transport
without monkeypatching. The default delegates to
`charter.mcp_server._fetch_and_verify`, which is the same function the
`fetch_charter` MCP tool calls internally — it returns a parsed
:class:`Charter` and raises a typed `CharterError` on any failure.

The grader callable (`hits_grader`) follows the same pattern as
`charter.adapters.openai_agents` so AP2 hosts can keep their LLM
traffic on whichever provider they already use.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, cast

from .._logging import get_logger
from ..errors import CharterError
from ..mcp_server import (
    _fetch_and_verify,
)
from ..mcp_server import (
    aggregate_verdict as _aggregate_verdict_tool,
)
from ..schema import AP2VerifyResult, Charter, Verdict

_log = get_logger("charter.adapters.ap2")

FetchCharterFn = Callable[[str], Charter]
HitsGrader = Callable[[Charter, str], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_fetch_charter(url: str) -> Charter:
    """Default `fetch_charter_fn`: delegate to `_fetch_and_verify`.

    Wrapped (rather than aliased) so the resulting symbol carries the
    `(str) -> Charter` signature without dragging in `_fetch_and_verify`'s
    internal logging context. Hosts can swap this for their own cached
    fetcher by passing `fetch_charter_fn=`.
    """
    return _fetch_and_verify(url)


def _default_grader() -> HitsGrader:
    """Lazy import of `charter.loopback._grade_via_llm`.

    Same trick the OpenAI Agents adapter uses — keeps the Anthropic
    import out of the hot path for hosts that supply their own grader.
    """
    from ..loopback import _grade_via_llm

    return _grade_via_llm


def _call_aggregate_verdict(charter: Charter, hits: list[dict[str, Any]]) -> Verdict:
    """Invoke `aggregate_verdict` past the @mcp.tool wrapper."""
    payload = charter.model_dump(mode="json")
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(_aggregate_verdict_tool, attr):
            raw = getattr(_aggregate_verdict_tool, attr)(payload, hits)
            return Verdict.model_validate(raw)
    raw = _aggregate_verdict_tool(payload, hits)
    return Verdict.model_validate(raw)


def _check_mandate(mandate: dict[str, Any]) -> tuple[bool, str]:
    """Mock AP2 mandate integrity check.

    Returns `(ok, reason)`. The production hook would call into the
    AP2 SDK to verify the signature against the payer's key, confirm
    the mandate is within its validity window, and confirm scope
    fields match the host's policy. v0.9 ships only the entry-point
    shape: a non-empty `signature` string passes; anything else fails.
    """
    if not isinstance(mandate, dict):
        return False, "mandate is not a dict"
    sig = mandate.get("signature")
    if not isinstance(sig, str) or not sig:
        return False, "mandate.signature is missing or empty"
    return True, "mandate signature accepted (mock check)"


def _extract_charter_url(mandate: dict[str, Any]) -> str | None:
    extensions = mandate.get("extensions")
    if not isinstance(extensions, dict):
        return None
    url = extensions.get("charter_url")
    if isinstance(url, str) and url:
        return url
    return None


def _extract_task(mandate: dict[str, Any]) -> str:
    """Pull the natural-language task description out of the mandate.

    Falls back to a synthesized description built from payer/payee/amount
    if the issuer didn't include an explicit `task` field — Charter's
    grader needs *some* text to reason about.
    """
    task = mandate.get("task")
    if isinstance(task, str) and task:
        return task

    payer = str(mandate.get("payer", "unknown payer"))
    payee = str(mandate.get("payee", "unknown payee"))
    amount = mandate.get("amount") or {}
    value = amount.get("value") if isinstance(amount, dict) else None
    currency = amount.get("currency") if isinstance(amount, dict) else None
    if value is not None and currency:
        return f"Pay {value} {currency} from {payer} to {payee}."
    return f"Payment from {payer} to {payee}."


def _decide(
    mandate_ok: bool,
    charter_verdict: Verdict | None,
) -> tuple[str, str]:
    """Apply the collapse rule. Returns `(final_decision, reason)`.

    Rule (from the issue spec):
        mandate_ok=False                                 -> incompatible
        mandate_ok=True, charter_verdict is None         -> incompatible
        mandate_ok=True, charter_verdict=incompatible    -> incompatible
        mandate_ok=True, charter_verdict=needs_approval  -> needs_approval
        mandate_ok=True, charter_verdict=allow           -> allow
    """
    if not mandate_ok:
        return "incompatible", "AP2 mandate integrity check failed."
    if charter_verdict is None:
        return (
            "incompatible",
            "Charter could not be fetched or verified; refusing despite valid mandate.",
        )
    if charter_verdict.decision == "incompatible":
        return (
            "incompatible",
            f"Charter compatibility check returned incompatible: {charter_verdict.reason}",
        )
    if charter_verdict.decision == "needs_approval":
        return (
            "needs_approval",
            f"Charter compatibility check requires approval: {charter_verdict.reason}",
        )
    return (
        "allow",
        f"Both AP2 mandate and Charter compatibility check passed. {charter_verdict.reason}",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def embed_charter_in_mandate(mandate: dict[str, Any], charter_url: str) -> dict[str, Any]:
    """Return a new mandate dict with `extensions.charter_url` set.

    Pure function: does not mutate the input. Any pre-existing keys in
    `extensions` are preserved; if `charter_url` was already present it
    is overwritten with the new value.

    Args:
        mandate:     The AP2 mandate dict to extend.
        charter_url: HTTPS URL of the Charter the calling agent operates
                     under. Will be fetched and verified by the verifier.

    Returns:
        A deep copy of `mandate` with `extensions.charter_url` set.
    """
    if not isinstance(mandate, dict):
        raise TypeError(f"mandate must be a dict, got {type(mandate).__name__}")
    if not isinstance(charter_url, str) or not charter_url:
        raise ValueError("charter_url must be a non-empty string")

    new_mandate = copy.deepcopy(mandate)
    extensions = new_mandate.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
    extensions["charter_url"] = charter_url
    new_mandate["extensions"] = extensions
    return cast(dict[str, Any], new_mandate)


def verify(
    mandate: dict[str, Any],
    *,
    fetch_charter_fn: FetchCharterFn = _default_fetch_charter,
    hits_grader: HitsGrader | None = None,
) -> AP2VerifyResult:
    """Verify an AP2 mandate AND its embedded Charter, collapse to one result.

    Steps, in order:

        1. Run the AP2 mandate integrity check (`_check_mandate`).
        2. Extract `extensions.charter_url`. If absent, treat as a
           non-Charter-aware mandate — `charter_verdict=None`, final
           decision driven only by step 1.
        3. Call `fetch_charter_fn(url)` and grade the mandate's task
           against the Charter's clauses with `hits_grader`. Aggregate
           into a Charter `Verdict`.
        4. Collapse `(mandate_ok, charter_verdict)` per the rule table
           in `_decide`.

    A Charter fetch / verify failure (any `CharterError` subclass) is
    caught and surfaced as `charter_verdict=None` so the caller does not
    have to handle exceptions on top of the result enum.

    No LLM call happens inside this function unless the caller did NOT
    provide `hits_grader`, in which case the default Anthropic grader is
    invoked exactly once.

    Args:
        mandate:          The AP2 mandate dict.
        fetch_charter_fn: Injectable Charter fetcher (default:
                          `_fetch_and_verify`).
        hits_grader:      Injectable per-clause hit grader (default:
                          `charter.loopback._grade_via_llm`).

    Returns:
        An :class:`AP2VerifyResult` carrying both layers' outcomes and
        the collapsed `final_decision`.
    """
    mandate_ok, mandate_reason = _check_mandate(mandate)

    if not mandate_ok:
        final, reason = _decide(mandate_ok, None)
        _log.info(
            "ap2 verify failed at mandate layer",
            extra={
                "outcome": "incompatible",
                "mandate_ok": False,
                "final_decision": final,
            },
        )
        return AP2VerifyResult(
            mandate_ok=False,
            charter_verdict=None,
            final_decision=cast(Any, final),
            reason=f"{reason} ({mandate_reason})",
        )

    charter_url = _extract_charter_url(mandate)
    if charter_url is None:
        # Mandate has no Charter binding at all — refuse rather than
        # silently allow. A Charter-aware verifier shouldn't be wired up
        # for mandates that opt out of the protocol.
        final, reason = _decide(True, None)
        _log.warning(
            "ap2 verify: mandate has no extensions.charter_url",
            extra={"outcome": "incompatible", "final_decision": final},
        )
        return AP2VerifyResult(
            mandate_ok=True,
            charter_verdict=None,
            final_decision=cast(Any, final),
            reason="Mandate carries no extensions.charter_url; cannot run Charter check.",
        )

    try:
        charter = fetch_charter_fn(charter_url)
    except CharterError as e:
        final, reason = _decide(True, None)
        _log.warning(
            "ap2 verify: charter fetch failed",
            extra={
                "outcome": "incompatible",
                "charter_url": charter_url,
                "error": f"{type(e).__name__}: {e}",
                "final_decision": final,
            },
        )
        return AP2VerifyResult(
            mandate_ok=True,
            charter_verdict=None,
            final_decision=cast(Any, final),
            reason=f"{reason} (charter fetch error: {type(e).__name__}: {e})",
        )

    task = _extract_task(mandate)
    grader = hits_grader or _default_grader()
    hits = grader(charter, task)
    charter_verdict = _call_aggregate_verdict(charter, hits)

    final, reason = _decide(True, charter_verdict)
    _log.info(
        "ap2 verify complete",
        extra={
            "outcome": final,
            "charter_id": charter.charter_id,
            "principal_id": charter.binding.principal_id,
            "agent_id": charter.binding.agent_id,
            "charter_decision": charter_verdict.decision,
            "final_decision": final,
        },
    )
    return AP2VerifyResult(
        mandate_ok=True,
        charter_verdict=charter_verdict,
        final_decision=cast(Any, final),
        reason=reason,
    )


__all__ = [
    "FetchCharterFn",
    "HitsGrader",
    "embed_charter_in_mandate",
    "verify",
]
