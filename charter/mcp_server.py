"""Charter MCP server exposing protocol-layer + agent-to-agent messaging tools.

Design intent (§P1-6, §P2-11):

    The MCP server itself does NOT call an LLM. The calling agent's own LLM
    is responsible for the *judgment* (which clauses are hit, with what
    confidence). The MCP server only:

        1. Fetches and verifies Charters (data access).
        2. Aggregates per-clause judgments deterministically using the
           TYPE_TO_DECISION map + the `incompatible > needs_approval > allow`
           precedence rule (protocol layer).
        3. Mediates agent-to-agent messaging via local inbox/outbox files.

    This makes the server stateless about credentials — no ANTHROPIC_API_KEY
    is required at runtime. The LLM judgment happens inside Codex (or any
    other MCP-capable calling agent) using whatever model it is already
    configured to use.

Tools:

    fetch_charter(charter_url)
        Data access. Returns Charter JSON + protocol_hints (TYPE_TO_DECISION
        map, aggregation rule, verdict schema, step-by-step instructions).

    aggregate_verdict(charter, hits)
        Protocol layer. Given the caller's per-clause hit/confidence/reason
        judgments, returns a structured Verdict with applied-clause markers.

    delegate_task(target_principal_id, target_agent_id, intended_task, from_agent?)
        Calling agent -> writes inbox.json so the target agent can pick it up.

    check_inbox()
        Target agent -> reads the most recent task sitting in inbox.json.

    send_result(task_id, verdict, response_text, executed?, execution_output?, from_agent?)
        Target agent -> writes outbox.json so the calling agent sees the reply.

    read_outbox()
        Calling agent -> reads the most recent reply in outbox.json.

Transport: stdio. Configure Codex via ~/.codex/config.toml:

    [mcp_servers.charter]
    command = "charter-mcp"

    [mcp_servers.charter.env]
    CHARTER_URL_BASE = "http://localhost:8000"
    CHARTER_DATA_DIR = "C:/path/to/agent contract/data"
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

# Use the FastMCP shim from Anthropic's official `mcp` Python SDK, not the
# standalone `fastmcp` package. The standalone package's 3.x release ships a
# different stdio protocol revision that OpenAI Codex CLI does not recognize,
# resulting in `Tools: (none)` even though tools are registered server-side.
# The official `mcp.server.fastmcp.FastMCP` provides the same decorator API
# and stays in lockstep with the canonical MCP protocol spec.
from mcp.server.fastmcp import FastMCP

from ._logging import get_logger
from .constants import (
    DEFAULT_URL_BASE,
    LOW_CONFIDENCE_THRESHOLD,
    TYPE_TO_DECISION,
    Decision,
    aggregate_decision,
)
from .errors import (
    CharterExpiredError,
    CharterKeyMismatchError,
    CharterNotFoundError,
    CharterPinMismatchError,
    CharterRevokedError,
    CharterSchemaError,
    CharterSignatureError,
)
from .keys import fetch_jwks, issuer_origin_from_url, jwk_to_public_key_string
from .observability import charter_span_cm, set_span_attrs
from .pins import fingerprint_of, get_pin, record_pin, update_last_verified
from .schema import Charter, MatchedClause, Verdict
from .signing import verify_charter
from .storage import data_root

_log = get_logger("charter.fetch")

mcp = FastMCP("charter")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _messages_dir() -> Path:
    path = data_root() / "messages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inbox_path() -> Path:
    return _messages_dir() / "inbox.json"


def _outbox_path() -> Path:
    return _messages_dir() / "outbox.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _fetch_and_verify(charter_url: str) -> Charter:
    """Fetch a Charter JSON, parse, verify signature, check lifecycle.

    Raises one of `charter.errors.CharterError` subclasses on failure:
        - `CharterNotFoundError`    — network failure or non-2xx HTTP response
        - `CharterSchemaError`      — body is not a valid Charter
        - `CharterSignatureError`   — issuer signature does not verify
        - `CharterKeyMismatchError` — `provenance.issuer_kid` is not the JWKS
                                      key, or JWKS key disagrees with inline
                                      `issuer_public_key`
        - `JWKSNotFoundError`       — Charter has a `kid` but JWKS unreachable
        - `JWKSParseError`          — Charter has a `kid` but JWKS is malformed
        - `CharterRevokedError`     — `lifecycle.status == "revoked"`
        - `CharterExpiredError`     — `lifecycle.status in {"expired", "superseded"}`

    Trust order (v0.8):
        1. Inline-key signature must verify (covers `kid` since `kid` is in the
           signed payload — a kid swap breaks this check).
        2. If Charter carries `issuer_kid`, fetch the issuer's JWKS and
           confirm the kid is published AND the JWKS key matches the inline
           `issuer_public_key`. This is what catches a forger who signs with
           their own key and lies about the issuer.
        3. Legacy Charters (no `kid`) skip step 2 and keep working under the
           v0 self-attesting trust model.

    Emits exactly one log line per call describing the outcome, and one
    OTel span (`charter.fetch_and_verify`) carrying `charter.id` /
    `charter.principal_id` / `charter.agent_id` / `charter.verdict` /
    `charter.cache_hit` / `charter.latency_ms` when OTel is installed.
    """
    start = time.monotonic()
    with charter_span_cm(
        "charter.fetch_and_verify",
        {"charter.url": charter_url, "charter.cache_hit": False},
    ) as span:
        try:
            charter = _fetch_and_verify_impl(charter_url)
        except Exception as e:
            # _fetch_and_verify_impl emits structured logs already; here we
            # only enrich the span before re-raising. charter_span_cm itself
            # records the exception and flips status to ERROR.
            set_span_attrs(
                span,
                {
                    "charter.verdict": type(e).__name__,
                    "charter.latency_ms": int((time.monotonic() - start) * 1000),
                },
            )
            raise

        set_span_attrs(
            span,
            {
                "charter.id": charter.charter_id,
                "charter.principal_id": charter.binding.principal_id,
                "charter.agent_id": charter.binding.agent_id,
                "charter.verdict": "ok",
                "charter.latency_ms": int((time.monotonic() - start) * 1000),
            },
        )
        return charter


def _fetch_and_verify_impl(charter_url: str) -> Charter:
    """Inner implementation. Kept separate from `_fetch_and_verify` so the
    span wrapper can capture timing + verdict cleanly without indenting the
    whole body three more levels. The order of operations is **unchanged**
    from the pre-B2.7 implementation — see protocol invariant #6.
    """
    try:
        resp = httpx.get(charter_url, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        _log.warning(
            "fetch failed: HTTP error",
            extra={
                "url": charter_url,
                "outcome": "not_found",
                "status_code": e.response.status_code,
            },
        )
        raise CharterNotFoundError(f"GET {charter_url} -> HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        _log.warning(
            "fetch failed: request error",
            extra={"url": charter_url, "outcome": "not_found", "error": str(e)},
        )
        raise CharterNotFoundError(f"GET {charter_url} failed: {e}") from e

    try:
        charter = Charter.model_validate(resp.json())
    except Exception as e:
        _log.warning(
            "fetch failed: schema error",
            extra={"url": charter_url, "outcome": "schema_error", "error": str(e)},
        )
        raise CharterSchemaError(f"Invalid Charter JSON at {charter_url}: {e}") from e

    if not verify_charter(charter):
        _log.error(
            "fetch failed: signature did not verify",
            extra={
                "url": charter_url,
                "charter_id": charter.charter_id,
                "outcome": "signature_error",
            },
        )
        raise CharterSignatureError(f"Bad signature at {charter_url}")

    log_ctx = {
        "url": charter_url,
        "charter_id": charter.charter_id,
        "principal_id": charter.binding.principal_id,
        "agent_id": charter.binding.agent_id,
    }

    # JWKS cross-check (v0.8+). Only runs when the Charter carries a kid.
    # Legacy charters fall back to the v0 self-attesting trust model.
    if charter.provenance.issuer_kid is not None:
        _check_jwks_consistency(charter, charter_url, log_ctx)

    # Fingerprint pinning (v0.8+). TOFU on first fetch; mismatch on
    # subsequent fetches is a hard failure.
    _check_pin(charter, log_ctx)

    status = charter.lifecycle.status
    if status == "revoked":
        _log.warning("fetch returned revoked charter", extra={**log_ctx, "outcome": "revoked"})
        raise CharterRevokedError(f"Charter status=revoked at {charter_url}")
    if status in ("expired", "superseded"):
        _log.warning(
            f"fetch returned {status} charter",
            extra={**log_ctx, "outcome": status},
        )
        raise CharterExpiredError(f"Charter status={status} at {charter_url}")

    _log.info("fetch ok", extra={**log_ctx, "outcome": "ok"})
    return charter


def _check_jwks_consistency(charter: Charter, charter_url: str, log_ctx: dict[str, Any]) -> None:
    """Confirm `charter.provenance.issuer_kid` matches an entry in the
    issuer's JWKS and that the JWKS key equals the inline public key.

    Raises:
        JWKSNotFoundError:        JWKS endpoint unreachable.
        JWKSParseError:           JWKS body malformed.
        CharterKeyMismatchError:  kid not in JWKS, or JWKS key != inline key.
    """
    kid = charter.provenance.issuer_kid
    assert kid is not None  # call site guarantees this

    origin = issuer_origin_from_url(charter_url)
    jwks = fetch_jwks(origin)  # may raise JWKSNotFoundError / JWKSParseError

    jwk = jwks.get(kid)
    if jwk is None:
        _log.warning(
            "fetch failed: kid not in JWKS",
            extra={**log_ctx, "kid": kid, "outcome": "key_mismatch"},
        )
        raise CharterKeyMismatchError(f"Charter kid={kid!r} not present in JWKS at {origin}")

    try:
        jwks_key_str = jwk_to_public_key_string(jwk)
    except ValueError as e:
        _log.warning(
            "fetch failed: JWKS entry malformed",
            extra={**log_ctx, "kid": kid, "outcome": "key_mismatch", "error": str(e)},
        )
        raise CharterKeyMismatchError(
            f"JWKS entry for kid={kid!r} is not a valid Ed25519 OKP key: {e}"
        ) from e

    if jwks_key_str != charter.provenance.issuer_public_key:
        _log.warning(
            "fetch failed: JWKS key != inline key",
            extra={**log_ctx, "kid": kid, "outcome": "key_mismatch"},
        )
        raise CharterKeyMismatchError(
            f"Charter inline issuer_public_key disagrees with JWKS for kid={kid!r}"
        )


def _check_pin(charter: Charter, log_ctx: dict[str, Any]) -> None:
    """TOFU-then-pin check against `data/pins.json`.

    Computes the fingerprint of `charter.provenance.issuer_public_key`,
    then compares against the persisted pin for `charter.binding.principal_id`.

      - No existing pin → record one (TOFU first fetch).
      - Fingerprint matches → refresh `last_verified` and continue.
      - Fingerprint differs → raise `CharterPinMismatchError`.

    The pin is keyed by `binding.principal_id`, which is the identity a
    calling agent cares about ("am I still talking to alice@acme.com?").
    """
    principal_id = charter.binding.principal_id
    current_fp = fingerprint_of(charter.provenance.issuer_public_key)

    pin = get_pin(principal_id)
    if pin is None:
        record_pin(principal_id, current_fp)
        return

    if pin.fingerprint != current_fp:
        _log.warning(
            "fetch failed: pin mismatch",
            extra={
                **log_ctx,
                "pinned_fingerprint": pin.fingerprint,
                "current_fingerprint": current_fp,
                "outcome": "pin_mismatch",
            },
        )
        raise CharterPinMismatchError(
            f"Pinned fingerprint for {principal_id!r} is {pin.fingerprint}, "
            f"current key fingerprints to {current_fp}. Run "
            f"`charter pins reset {principal_id}` after a legitimate rotation."
        )

    update_last_verified(principal_id)


# ---------------------------------------------------------------------------
# Tool 1: fetch_charter (data access + protocol hints)
# ---------------------------------------------------------------------------


@mcp.tool()
def fetch_charter(charter_url: str) -> dict[str, Any]:
    """Fetch a Charter by URL, verify its signature, return the JSON + hints.

    The `protocol_hints` field tells the caller's LLM exactly how to reason:
    apply TYPE_TO_DECISION per-clause, then call `aggregate_verdict` to get a
    structured Verdict deterministically.

    Args:
        charter_url: e.g. "http://localhost:8000/alice@acme.com/research_agent_v1"

    Returns:
        {
          "charter":        <Charter JSON>,
          "protocol_hints": {
            "type_to_decision": {scope: allow, out_of_scope: incompatible, ...},
            "aggregation_rule": "incompatible > needs_approval > allow",
            "verdict_schema":  <expected shape>,
            "instructions":    <how the calling LLM should reason>
          }
        }
    """
    charter = _fetch_and_verify(charter_url)

    return {
        "charter": charter.model_dump(mode="json"),
        "protocol_hints": {
            "type_to_decision": dict(TYPE_TO_DECISION),
            "aggregation_rule": (
                "incompatible > needs_approval > allow. "
                "If no clause is hit, default to needs_approval. "
                f"If every hit has confidence < {LOW_CONFIDENCE_THRESHOLD}, "
                "default to needs_approval."
            ),
            "verdict_schema": {
                "decision": "allow | needs_approval | incompatible",
                "matched_clauses": ("list of {id, local_decision, applied, confidence, reason}"),
                "reason": "string -- short summary referencing applied clauses",
                "rewrite_available": "bool",
            },
            "instructions": (
                "For each clause in charter.clauses[], decide whether the "
                "intended task is HIT by it. Output {id, hit:bool, "
                "confidence:float 0..1, reason:str} per clause you mark hit. "
                "Be strict on out_of_scope; conservative on approval_required. "
                "Then call aggregate_verdict(charter, hits) -- the protocol "
                "layer will apply TYPE_TO_DECISION and precedence to produce "
                "the final structured Verdict."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Tool 2: aggregate_verdict (protocol layer, no LLM)
# ---------------------------------------------------------------------------


@mcp.tool()
def aggregate_verdict(charter: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-clause hits into a structured Verdict.

    Args:
        charter: The Charter dict as returned by fetch_charter()["charter"].
        hits:    A list of per-clause judgments produced by the calling LLM.
                 Each entry: {id, hit:bool, confidence:float 0..1, reason:str}.
                 Entries with hit=False are ignored.

    Returns:
        Verdict {decision, matched_clauses[], reason, rewrite_available}.

    Determinism: this function makes no LLM call. Given the same charter +
    hits it always returns the same Verdict.
    """
    clauses_data = charter.get("clauses", []) or []
    type_by_id: dict[str, str] = {c["id"]: c["type"] for c in clauses_data}

    matched: list[MatchedClause] = []
    hit_decisions: list[Decision] = []

    for h in hits:
        if not h.get("hit", False):
            continue
        cid = h.get("id")
        if not isinstance(cid, str) or cid not in type_by_id:
            continue
        clause_type = type_by_id[cid]
        local = TYPE_TO_DECISION[clause_type]
        confidence = float(h.get("confidence", 0.0))
        matched.append(
            MatchedClause(
                id=cid,
                local_decision=local,
                applied=False,
                confidence=confidence,
                reason=h.get("reason", ""),
            )
        )
        hit_decisions.append(local)

    # 0-match fallback
    if not matched:
        verdict = Verdict(
            decision="needs_approval",
            matched_clauses=[],
            reason=(
                "No clauses matched the intended task; defaulting to "
                "needs_approval as a conservative fallback."
            ),
            rewrite_available=False,
        )
        return verdict.model_dump(mode="json")

    # Low-confidence fallback (P2-11 edge case)
    if all(m.confidence < LOW_CONFIDENCE_THRESHOLD for m in matched):
        for m in matched:
            m.applied = True
        verdict = Verdict(
            decision="needs_approval",
            matched_clauses=matched,
            reason=(
                f"All matched clauses have low confidence "
                f"(<{LOW_CONFIDENCE_THRESHOLD}). Defaulting to needs_approval."
            ),
            rewrite_available=False,
        )
        return verdict.model_dump(mode="json")

    # Normal aggregation
    decision = aggregate_decision(hit_decisions)
    for m in matched:
        if m.local_decision == decision:
            m.applied = True
    applied_ids = [m.id for m in matched if m.applied]
    reason = f"Aggregate decision '{decision}' from applied clauses: {', '.join(applied_ids)}."

    # rewrite_available iff incompatible AND at least one out_of_scope was hit
    rewrite_available = decision == "incompatible" and any(
        isinstance(h.get("id"), str)
        and type_by_id.get(cast(str, h["id"])) == "out_of_scope"
        and h.get("hit")
        for h in hits
    )

    verdict = Verdict(
        decision=decision,
        matched_clauses=matched,
        reason=reason,
        rewrite_available=rewrite_available,
    )
    return verdict.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool 3: delegate_task (calling agent -> inbox)
# ---------------------------------------------------------------------------


@mcp.tool()
def delegate_task(
    target_principal_id: str,
    target_agent_id: str,
    intended_task: str,
    from_agent: str = "claude_code",
) -> dict[str, Any]:
    """Send a task to the target agent by writing to the inbox file.

    The target agent should call check_inbox() to receive it.

    Args:
        target_principal_id: e.g. "alice@acme.com"
        target_agent_id:     e.g. "research_agent_v1"
        intended_task:       the task description in natural language.
        from_agent:          identifier for the calling agent (default
                             "claude_code").

    Returns:
        {ok, task_id, charter_url, inbox_path, delivered_to, next}
    """
    task_id = uuid.uuid4().hex[:8]
    base = os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE).rstrip("/")
    charter_url = f"{base}/{target_principal_id}/{target_agent_id}"

    msg = {
        "task_id": task_id,
        "sent_at": _now_iso(),
        "from_agent": from_agent,
        "target_principal_id": target_principal_id,
        "target_agent_id": target_agent_id,
        "charter_url": charter_url,
        "intended_task": intended_task,
        "status": "pending",
    }

    _write_json(_inbox_path(), msg)

    return {
        "ok": True,
        "task_id": task_id,
        "charter_url": charter_url,
        "inbox_path": str(_inbox_path()),
        "delivered_to": f"{target_principal_id} x {target_agent_id}",
        "next": (
            "Task delivered. The target agent should call check_inbox() to "
            "receive it. Use read_outbox() to see the response once the "
            "target writes one."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 4: check_inbox (target agent reads pending task)
# ---------------------------------------------------------------------------


@mcp.tool()
def check_inbox() -> dict[str, Any] | None:
    """Read the most recent task delivered to this target agent.

    Returns None if the inbox is empty.
    """
    return _read_json(_inbox_path())


# ---------------------------------------------------------------------------
# Tool 5: send_result (target agent -> outbox)
# ---------------------------------------------------------------------------


@mcp.tool()
def send_result(
    task_id: str,
    verdict: dict[str, Any],
    response_text: str,
    executed: bool = False,
    execution_output: str | None = None,
    from_agent: str = "codex",
) -> dict[str, Any]:
    """Write the target agent's reply to the outbox file.

    The calling agent will see it via read_outbox().

    Args:
        task_id:          the task_id from check_inbox().
        verdict:          the Verdict dict (as returned by aggregate_verdict).
        response_text:    natural-language reply to the calling agent.
        executed:         True iff the target actually performed the work.
        execution_output: any artifact produced (code, data, etc.).
        from_agent:       identifier (default "codex").
    """
    msg = {
        "task_id": task_id,
        "responded_at": _now_iso(),
        "from_agent": from_agent,
        "verdict": verdict,
        "response_text": response_text,
        "executed": executed,
        "execution_output": execution_output,
    }
    _write_json(_outbox_path(), msg)
    return {"ok": True, "outbox_path": str(_outbox_path())}


# ---------------------------------------------------------------------------
# Tool 6: read_outbox (calling agent reads target's reply)
# ---------------------------------------------------------------------------


@mcp.tool()
def read_outbox() -> dict[str, Any] | None:
    """Read the most recent reply from the target agent.

    Returns None if the outbox is empty.
    """
    return _read_json(_outbox_path())


# ---------------------------------------------------------------------------
# Tool 7: propose_within_scope (protocol layer; one LLM call)
# ---------------------------------------------------------------------------


@mcp.tool()
def propose_within_scope(
    charter_url: str,
    intended_task: str,
    failed_verdict: dict[str, Any],
) -> dict[str, Any]:
    """Suggest an in-scope rewrite of a task that failed compatibility check.

    Call this only when `aggregate_verdict` returned
    `decision=incompatible` AND `rewrite_available=true`. Given the same
    Charter, the original task, and the failed Verdict, this returns a
    nearby task that DOES fit the Charter's scope.

    This tool makes one LLM call (the only LLM call any charter MCP tool
    makes). The single-shot path is what this iteration ships; loopback
    verification with retry will wrap this in a future iteration.

    Args:
        charter_url:    The same URL that was passed to `fetch_charter`.
        intended_task:  The original natural-language task description.
        failed_verdict: The Verdict dict returned by `aggregate_verdict`.

    Returns:
        On success: {"ok": true, "proposal": {<RewriteProposal fields>}}.
        On no-rewrite-feasible: {"ok": false, "reason": "no viable rewrite"}.
        On missing API key:     {"ok": false, "reason": "no LLM configured"}.
    """
    from .propose import propose_within_scope_llm
    from .schema import Verdict

    try:
        verdict = Verdict.model_validate(failed_verdict)
    except Exception as e:
        return {"ok": False, "reason": f"failed_verdict is not a valid Verdict: {e}"}

    charter = _fetch_and_verify(charter_url)

    try:
        proposal = propose_within_scope_llm(charter, intended_task, verdict)
    except RuntimeError as e:
        # Missing API key — surface a clean degraded response.
        return {"ok": False, "reason": str(e)}

    if proposal is None:
        return {"ok": False, "reason": "no viable rewrite within Charter scope"}

    return {"ok": True, "proposal": proposal.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Tool 8: propose_within_scope_verified (loopback wrapper around tool 7)
# ---------------------------------------------------------------------------


@mcp.tool()
def propose_within_scope_verified(
    charter_url: str,
    intended_task: str,
    failed_verdict: dict[str, Any],
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Generate a rewrite, then verify it grades as `allow` — retry if not.

    This is the loopback wrapper around `propose_within_scope`. Each
    attempt: generate a rewrite at annealed temperature, ask the LLM to
    grade the rewrite against the Charter's clauses, run
    `aggregate_verdict` on the resulting hits. If the verdict is `allow`,
    return the rewrite. Otherwise feed the failure back into the next
    attempt's prompt and retry, up to `max_attempts`.

    Cost note: this is the only MCP tool that makes multiple LLM calls per
    invocation — up to `2 × max_attempts`. Calling agents that prefer
    minimal server-side LLM cost should use `propose_within_scope`
    (single-shot) and run their own grading loop.

    Returns:
        Success:  {"ok": true, "proposal": {<RewriteProposal>}, "attempts": N}
        Failure:  {"ok": false, "reason": "...", "history": [<RewriteAttempt>...]}
        No-key:   {"ok": false, "reason": "ANTHROPIC_API_KEY not set"}
    """
    from .loopback import propose_within_scope_verified as _verified
    from .schema import RewriteFailure, Verdict

    try:
        verdict = Verdict.model_validate(failed_verdict)
    except Exception as e:
        return {"ok": False, "reason": f"failed_verdict is not a valid Verdict: {e}"}

    charter = _fetch_and_verify(charter_url)

    try:
        result = _verified(charter, intended_task, verdict, max_attempts=max_attempts)
    except RuntimeError as e:
        return {"ok": False, "reason": str(e)}

    if isinstance(result, RewriteFailure):
        return {
            "ok": False,
            "reason": result.reason,
            "history": [a.model_dump(mode="json") for a in result.attempts],
        }

    # Success — figure out which attempt landed it (last attempt is the winning one).
    # We don't carry the full history on success here; that's `history` is for failure.
    return {"ok": True, "proposal": result.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Tool 9: fetch_charter_chain (multi-hop attenuation walk)
# ---------------------------------------------------------------------------


_log_chain_fetch = get_logger("charter.fetch.chain")


@mcp.tool()
def fetch_charter_chain(charter_url: str, max_depth: int = 5) -> dict[str, Any]:
    """Walk parent_charter_url to the root, verifying each hop's
    signature, lifecycle, AND attenuation relationship.

    Returns the chain root-first (so chain[0] is the topmost principal
    and chain[-1] is the leaf you asked for). On any failure — broken
    signature, broken attenuation, cycle, or depth exceeded — returns
    `{"ok": false, "reason": "...", "partial": [...]}` with the
    Charters successfully verified up to that point.

    Args:
        charter_url:  The leaf Charter's URL. The walk goes upward.
        max_depth:    Bound on chain length. Default 5; minimum 1.

    Returns:
        Success:  {"ok": true, "chain": [<root>, ..., <leaf>], "depth": N}
        Failure:  {"ok": false, "reason": str, "partial": [...verified hops]}
    """
    from .chain import verify_chain

    with charter_span_cm(
        "charter.fetch_chain",
        {"charter.url": charter_url, "charter.max_depth": max_depth},
    ) as chain_span:
        if max_depth < 1:
            set_span_attrs(chain_span, {"charter.verdict": "bad_max_depth"})
            return {"ok": False, "reason": "max_depth must be >= 1", "partial": []}

        # leaf-first walk; we reverse at the end so the caller sees root-first.
        walked: list[Charter] = []
        seen_ids: set[str] = set()

        current_url = charter_url
        while True:
            if len(walked) >= max_depth:
                set_span_attrs(
                    chain_span,
                    {"charter.verdict": "max_depth_exceeded", "charter.chain_depth": len(walked)},
                )
                return _chain_failure(
                    f"max_depth={max_depth} exceeded while walking from {charter_url}",
                    walked,
                )

            try:
                charter = _fetch_and_verify(current_url)
            except Exception as e:
                # Typed errors from _fetch_and_verify propagate as a clean
                # failure rather than tearing the tool down. The error class
                # name is informative enough for the caller.
                set_span_attrs(
                    chain_span,
                    {
                        "charter.verdict": type(e).__name__,
                        "charter.chain_depth": len(walked),
                    },
                )
                return _chain_failure(
                    f"{type(e).__name__}: {e}",
                    walked,
                )

            # Cycle detection: refuse to revisit a charter_id.
            if charter.charter_id in seen_ids:
                set_span_attrs(
                    chain_span,
                    {"charter.verdict": "cycle", "charter.chain_depth": len(walked)},
                )
                return _chain_failure(
                    f"cycle detected at {charter.charter_id}",
                    walked,
                )
            seen_ids.add(charter.charter_id)

            walked.append(charter)

            if charter.parent_charter_url is None:
                break  # reached the root
            current_url = charter.parent_charter_url

        # walked is leaf-to-root; verify_chain wants child-then-parent, then we
        # produce a root-first output for the caller.
        for i in range(len(walked) - 1):
            child = walked[i]
            parent = walked[i + 1]
            if not verify_chain(child, parent):
                set_span_attrs(
                    chain_span,
                    {
                        "charter.verdict": "attenuation_broken",
                        "charter.chain_depth": len(walked),
                    },
                )
                return _chain_failure(
                    f"attenuation broken: {child.charter_id} is not a valid"
                    f" subset of {parent.charter_id}",
                    walked,
                )

        chain_root_first = list(reversed(walked))
        set_span_attrs(
            chain_span,
            {
                "charter.verdict": "ok",
                "charter.chain_depth": len(chain_root_first),
                "charter.chain_root_id": chain_root_first[0].charter_id,
                "charter.chain_leaf_id": chain_root_first[-1].charter_id,
            },
        )
        _log_chain_fetch.info(
            "chain fetched",
            extra={
                "root_charter_id": chain_root_first[0].charter_id,
                "leaf_charter_id": chain_root_first[-1].charter_id,
                "depth": len(chain_root_first),
                "outcome": "ok",
            },
        )
        return {
            "ok": True,
            "chain": [c.model_dump(mode="json") for c in chain_root_first],
            "depth": len(chain_root_first),
        }


# ---------------------------------------------------------------------------
# Tool 10: aggregate_verdict_chain (cross-Charter aggregation)
# ---------------------------------------------------------------------------


@mcp.tool()
def aggregate_verdict_chain(
    chain: list[dict[str, Any]],
    hits_per_charter: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Combine per-clause hits ACROSS a Charter Chain into one Verdict.

    Inputs:
        chain:
            The chain as returned by `fetch_charter_chain` — a list of
            Charter dicts root-first. Each entry must include `charter_id`
            and `clauses[]`.
        hits_per_charter:
            Map from `charter_id` to the same `hits[]` shape that the
            single-Charter `aggregate_verdict` accepts. Each Charter in
            the chain looks up its hits here; missing or empty entries
            simply mean that Charter contributes no matched clauses.

    Aggregation rule:
        The same `incompatible > needs_approval > allow` precedence as
        single-Charter, but applied across all matched clauses from
        all Charters in the chain. The strictest Charter wins; the
        `applied` flag is set on every matched_clause whose
        local_decision matches the final aggregate.

    Output:
        A `Verdict` with each `matched_clauses` entry carrying
        `source_charter_id` so the caller can see which Charter forced
        the outcome. The `reason` string names the applied clauses
        with `<source_charter_id>::<clause_id>` qualifiers.

    Determinism: no LLM call. Same chain + same hits => same Verdict.
    """
    with charter_span_cm(
        "charter.aggregate_chain",
        {"charter.chain_depth": len(chain)},
    ) as span:
        result = _aggregate_verdict_chain_impl(chain, hits_per_charter)
        # Surface useful summary stats on the span. matched_clauses lives in
        # the Verdict for the success path; for early-exit paths (empty chain,
        # no-match) it may be absent.
        matched_clauses = result.get("matched_clauses") or []
        applied_count = sum(1 for m in matched_clauses if m.get("applied"))
        set_span_attrs(
            span,
            {
                "charter.verdict": str(result.get("decision") or result.get("reason") or ""),
                "charter.matched_clause_count": len(matched_clauses),
                "charter.applied_clause_count": applied_count,
            },
        )
        return result


def _aggregate_verdict_chain_impl(
    chain: list[dict[str, Any]],
    hits_per_charter: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Inner implementation of aggregate_verdict_chain. Pulled out so the
    span wrapper can capture summary stats on the result without indenting
    the whole body. Aggregation semantics are **unchanged**."""
    if not chain:
        return {
            "ok": False,
            "reason": "empty chain",
        }

    matched: list[MatchedClause] = []
    hit_decisions: list[Decision] = []

    for charter_dict in chain:
        cid_source = charter_dict.get("charter_id")
        clauses_data = charter_dict.get("clauses", []) or []
        type_by_id: dict[str, str] = {c["id"]: c["type"] for c in clauses_data}

        hits = hits_per_charter.get(cid_source or "", []) or []
        for h in hits:
            if not h.get("hit", False):
                continue
            cid = h.get("id")
            if not isinstance(cid, str) or cid not in type_by_id:
                continue
            clause_type = type_by_id[cid]
            local = TYPE_TO_DECISION[clause_type]
            confidence = float(h.get("confidence", 0.0))
            matched.append(
                MatchedClause(
                    id=cid,
                    local_decision=local,
                    applied=False,
                    confidence=confidence,
                    reason=h.get("reason", ""),
                    source_charter_id=cid_source if isinstance(cid_source, str) else None,
                )
            )
            hit_decisions.append(local)

    # 0-match fallback — protocol consistency with single-Charter path.
    if not matched:
        verdict = Verdict(
            decision="needs_approval",
            matched_clauses=[],
            reason=(
                "No clauses matched anywhere in the chain; defaulting to "
                "needs_approval as a conservative fallback."
            ),
            rewrite_available=False,
        )
        return verdict.model_dump(mode="json")

    # Low-confidence fallback (same threshold as single-Charter).
    if all(m.confidence < LOW_CONFIDENCE_THRESHOLD for m in matched):
        for m in matched:
            m.applied = True
        verdict = Verdict(
            decision="needs_approval",
            matched_clauses=matched,
            reason=(
                f"All matched clauses across the chain have low confidence "
                f"(<{LOW_CONFIDENCE_THRESHOLD}). Defaulting to needs_approval."
            ),
            rewrite_available=False,
        )
        return verdict.model_dump(mode="json")

    # Normal aggregation: strictest wins, regardless of which Charter
    # in the chain it came from.
    decision = aggregate_decision(hit_decisions)
    for m in matched:
        if m.local_decision == decision:
            m.applied = True
    applied_refs = [f"{m.source_charter_id or '?'}::{m.id}" for m in matched if m.applied]
    reason = (
        f"Aggregate decision '{decision}' from applied clauses across the chain: "
        f"{', '.join(applied_refs)}."
    )

    # rewrite_available iff incompatible AND at least one out_of_scope hit anywhere.
    type_by_id_per_charter: dict[str, dict[str, str]] = {
        (c.get("charter_id") or ""): {cl["id"]: cl["type"] for cl in (c.get("clauses") or [])}
        for c in chain
    }
    rewrite_available = decision == "incompatible" and any(
        isinstance(h.get("id"), str)
        and type_by_id_per_charter.get(cid, {}).get(cast(str, h["id"])) == "out_of_scope"
        and h.get("hit")
        for cid, hits in hits_per_charter.items()
        for h in hits
    )

    verdict = Verdict(
        decision=decision,
        matched_clauses=matched,
        reason=reason,
        rewrite_available=rewrite_available,
    )
    return verdict.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool 11: verify_chain_semantic (LLM-using; per ADR-009 declared explicitly)
# ---------------------------------------------------------------------------


@mcp.tool()
def verify_chain_semantic(
    child_charter_url: str,
    parent_charter_url: str,
) -> dict[str, Any]:
    """LLM-based semantic subset check between two fetched Charters.

    **LLM-using tool (ADR-009 exception).** Unlike `verify_chain` in
    strict mode (which is pure string matching), this tool calls the
    Anthropic API one or more times to ask whether each parent
    restriction is semantically covered by the child's clauses. Use
    this when string-based `fetch_charter_chain` rejects a child that
    is *known* to be a legitimate reword of its parent.

    Determinism note: results are written into
    `child.attenuation_proof.semantic_check_cache` so subsequent calls
    against the same (child, parent_revision) pair short-circuit
    without invoking the LLM. The MCP server does NOT hold the issuer
    private key, so the in-memory cache update CANNOT be persisted +
    re-signed at this layer — callers that want a signed cache must
    use the Python API (`charter.chain.verify_chain_semantic` with a
    `signer_private_key`) on the issuer side.

    Args:
        child_charter_url:  URL of the (claimed) attenuating Charter.
        parent_charter_url: URL of the parent Charter.

    Returns:
        Success:
            {"ok": true, "matches_subset": bool, "reason": str,
             "cache_key": str}
        Grader / fetch failure:
            {"ok": false, "reason": str}
    """
    from .chain import _semantic_cache_key
    from .chain import verify_chain_semantic as _verify_semantic
    from .errors import CharterChainGraderError

    try:
        child = _fetch_and_verify(child_charter_url)
        parent = _fetch_and_verify(parent_charter_url)
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    try:
        # No signer_private_key: the MCP layer can't sign, so the cache
        # entry lives only in this response — callers are expected to
        # round-trip back to the issuer if they want it persisted.
        matches = _verify_semantic(child, parent, grader_client=None)  # type: ignore[arg-type]
    except CharterChainGraderError as e:
        return {"ok": False, "reason": f"grader_failure: {e}"}
    except ValueError as e:
        # Raised when no grader_client was passed AND ANTHROPIC_API_KEY is
        # missing. Surface as a clean degraded response.
        return {"ok": False, "reason": str(e)}

    cache_key = _semantic_cache_key(parent)
    # After the call we know the verdict was just written into the cache
    # (or fetched from it); read the reason back to surface to the caller.
    assert child.attenuation_proof is not None  # verify_chain_semantic ensures this
    cached = child.attenuation_proof.semantic_check_cache.get(cache_key)
    reason = cached.reason if cached is not None else ""

    return {
        "ok": True,
        "matches_subset": matches,
        "reason": reason,
        "cache_key": cache_key,
    }


def _chain_failure(reason: str, walked_leaf_first: list[Charter]) -> dict[str, Any]:
    """Emit a failure log + build the partial-chain response."""
    partial = list(reversed(walked_leaf_first))
    _log_chain_fetch.warning(
        "chain fetch failed",
        extra={
            "reason": reason,
            "depth": len(partial),
            "outcome": "failed",
        },
    )
    return {
        "ok": False,
        "reason": reason,
        "partial": [c.model_dump(mode="json") for c in partial],
    }


# ---------------------------------------------------------------------------
# Tool 12: request_step_up (B2.5 — HTTP forward only, no LLM)
# ---------------------------------------------------------------------------


@mcp.tool()
def request_step_up(
    charter_url: str,
    task: str,
    justification: str,
    max_ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Request a short-TTL AdHocGrant to widen authority for one task.

    The dual of `propose_within_scope`. Use this when the caller is
    confident the task MUST happen even though the Charter says it is
    out-of-scope or needs_approval — the caller submits the task and a
    justification, and the issuer's `POST /step-up` endpoint decides
    whether to mint a signed grant.

    **This tool MAY require human-in-the-loop approval depending on
    issuer policy.** The reference server's three modes are:
      - `auto-deny` (default): always returns `denied`.
      - `auto-approve`: signs and returns immediately.
      - `callback`: forwards to an external system (Slack / email /
        approval UI) and echoes the response.

    Per ADR-009, this MCP tool itself does NOT call an LLM. It only
    HTTP-forwards to the server-side `/step-up` endpoint, which then
    applies the configured approval policy.

    The server URL is composed from `CHARTER_URL_BASE` (or the env
    default), so a calling agent does not need to know the issuer's
    host beyond what the Charter already exposes.

    Args:
        charter_url:     URL of the Charter the grant attaches to.
        task:            Natural-language task description.
        justification:   Reason the principal should approve. Shown to
                         the human approver in callback mode.
        max_ttl_seconds: Requested TTL. Server caps at
                         `CHARTER_STEPUP_MAX_TTL` (default 3600).

    Returns:
        AdHocGrantResponse JSON: `{status, grant?, denial_reason?}`.
        Network failures surface as `{ok: false, reason: ...}` so the
        caller can distinguish "issuer denied" from "could not reach
        issuer".
    """
    base = os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE).rstrip("/")
    endpoint = f"{base}/step-up"
    body = {
        "charter_url": charter_url,
        "task": task,
        "justification": justification,
        "max_ttl_seconds": max_ttl_seconds,
    }
    try:
        resp = httpx.post(endpoint, json=body, timeout=15.0)
    except httpx.RequestError as e:
        return {"ok": False, "reason": f"could not reach {endpoint}: {e}"}

    if resp.status_code >= 400:
        # Surface server-side validation / rate-limit errors verbatim
        # so the calling agent can decide whether to retry.
        try:
            detail = resp.json()
        except Exception:
            detail = {"detail": resp.text}
        return {
            "ok": False,
            "reason": f"HTTP {resp.status_code}",
            "detail": detail,
        }

    try:
        payload: dict[str, Any] = resp.json()
    except Exception as e:
        return {"ok": False, "reason": f"server returned non-JSON body: {e}"}
    return payload


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Console-script entry point: `charter-mcp`. Speaks stdio MCP."""
    mcp.run()


if __name__ == "__main__":
    run()
