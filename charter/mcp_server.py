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
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
# Use the FastMCP shim from Anthropic's official `mcp` Python SDK, not the
# standalone `fastmcp` package. The standalone package's 3.x release ships a
# different stdio protocol revision that OpenAI Codex CLI does not recognize,
# resulting in `Tools: (none)` even though tools are registered server-side.
# The official `mcp.server.fastmcp.FastMCP` provides the same decorator API
# and stays in lockstep with the canonical MCP protocol spec.
from mcp.server.fastmcp import FastMCP

from .constants import (
    DEFAULT_URL_BASE,
    LOW_CONFIDENCE_THRESHOLD,
    TYPE_TO_DECISION,
    aggregate_decision,
)
from .schema import Charter, MatchedClause, Verdict
from .signing import verify_charter
from .storage import data_root


mcp = FastMCP("charter")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _messages_dir():
    path = data_root() / "messages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inbox_path():
    return _messages_dir() / "inbox.json"


def _outbox_path():
    return _messages_dir() / "outbox.json"


def _write_json(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json(path) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_and_verify(charter_url: str) -> Charter:
    """Fetch a Charter JSON, parse, verify signature, check lifecycle.

    Raises ValueError on any failure.
    """
    try:
        resp = httpx.get(charter_url, timeout=10.0)
        resp.raise_for_status()
    except Exception as e:
        raise ValueError(f"CharterNotFoundError: GET {charter_url} failed: {e}")

    try:
        charter = Charter.model_validate(resp.json())
    except Exception as e:
        raise ValueError(f"Invalid Charter JSON at {charter_url}: {e}")

    if not verify_charter(charter):
        raise ValueError(f"CharterSignatureError: bad signature at {charter_url}")

    status = charter.lifecycle.status
    if status in ("expired", "revoked", "superseded"):
        raise ValueError(f"CharterExpiredError: status={status}")

    return charter


# ---------------------------------------------------------------------------
# Tool 1: fetch_charter (data access + protocol hints)
# ---------------------------------------------------------------------------

@mcp.tool()
def fetch_charter(charter_url: str) -> dict:
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
                "matched_clauses": (
                    "list of {id, local_decision, applied, confidence, reason}"
                ),
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
def aggregate_verdict(charter: dict, hits: list[dict]) -> dict:
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
    type_by_id = {c["id"]: c["type"] for c in clauses_data}

    matched: list[MatchedClause] = []
    hit_decisions: list[str] = []

    for h in hits:
        if not h.get("hit", False):
            continue
        cid = h.get("id")
        if cid not in type_by_id:
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
    reason = (
        f"Aggregate decision '{decision}' from applied clauses: "
        f"{', '.join(applied_ids)}."
    )

    # rewrite_available iff incompatible AND at least one out_of_scope was hit
    rewrite_available = decision == "incompatible" and any(
        type_by_id.get(h.get("id")) == "out_of_scope" and h.get("hit")
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
) -> dict:
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
def check_inbox() -> Optional[dict]:
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
    verdict: dict,
    response_text: str,
    executed: bool = False,
    execution_output: Optional[str] = None,
    from_agent: str = "codex",
) -> dict:
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
def read_outbox() -> Optional[dict]:
    """Read the most recent reply from the target agent.

    Returns None if the outbox is empty.
    """
    return _read_json(_outbox_path())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """Console-script entry point: `charter-mcp`. Speaks stdio MCP."""
    mcp.run()


if __name__ == "__main__":
    run()
