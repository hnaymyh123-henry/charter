"""Cookbook #06 — Charter-gate Anthropic / Claude Agent SDK calls.

The full Anthropic adapter is deferred (per ADR-012 — OpenAI Agents
shipped in v0.7, Anthropic is on the v0.9+ backlog as low priority).
This cookbook shows the recommended hook points so you can build a
local equivalent of `charter.adapters.openai_agents.charter_gated` for
the Anthropic SDK in ~50 LOC.

The example uses a FakeAnthropic stand-in (real shape mirrors
`Anthropic().messages.create(...)` and `tool_use` blocks) so it runs
without an API key. The Charter gate sits at the *tool-execution*
boundary inside the agent loop — the same shape you'd use with a real
`anthropic` SDK call.

Run from the repo root:

    python examples/cookbook/06-integrate-anthropic-sdk/main.py
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Fake Anthropic SDK: just enough to emulate a tool-use loop.
# ---------------------------------------------------------------------------


class _FakeBlock:
    def __init__(self, type_: str, **kw: Any) -> None:
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, content: list[_FakeBlock], stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class FakeAnthropic:
    """Stand-in for the official Anthropic Python SDK.

    The real call shape is:
        client = anthropic.Anthropic()
        resp = client.messages.create(model=..., messages=..., tools=...)
        for block in resp.content:
            if block.type == "tool_use":
                ...

    This fake returns a single tool_use block per call so the cookbook
    can exercise the gate around the tool body.
    """

    def __init__(self, tool_use_calls: list[dict[str, Any]]) -> None:
        # Sequence of (tool_name, tool_input) the fake "model" will request.
        self._queue = list(tool_use_calls)

    def messages_create(self, *, model: str, messages: list[dict[str, Any]]) -> _FakeResponse:
        if not self._queue:
            return _FakeResponse([_FakeBlock("text", text="(no more tool calls queued)")])
        call = self._queue.pop(0)
        return _FakeResponse(
            [
                _FakeBlock(
                    "tool_use",
                    id=f"toolu_{len(messages):04d}",
                    name=call["name"],
                    input=call["input"],
                )
            ],
            stop_reason="tool_use",
        )


# ---------------------------------------------------------------------------
# The Charter gate. This is the function the (future) adapter will provide.
# ---------------------------------------------------------------------------


def charter_preflight_inline(
    *,
    principal_id: str,
    agent_id: str,
    task: str,
    grader: Callable[[Any, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Stand-in for charter.adapters.openai_agents.charter_preflight.

    A future charter.adapters.anthropic.charter_preflight will look
    almost identical, just typed to fit Anthropic's tool-use block
    shape.
    """
    from charter.mcp_server import aggregate_verdict
    from charter.storage import load_charter

    charter = load_charter(principal_id, agent_id)
    if charter is None:
        return {"decision": "incompatible", "reason": "no Charter on disk"}
    hits = grader(charter, task)
    fn = getattr(aggregate_verdict, "fn", aggregate_verdict)
    verdict = fn(charter.model_dump(mode="json"), hits)
    return verdict


def charter_gated_tool(
    *,
    principal_id: str,
    agent_id: str,
    grader: Callable[[Any, str], list[dict[str, Any]]],
    refuse_on: tuple[str, ...] = ("incompatible", "needs_approval"),
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Hand-rolled decorator equivalent of charter_gated for Anthropic.

    Drop this into a `charter/adapters/anthropic.py` module to ship as
    a real adapter; the only differences vs the OpenAI Agents version
    are imports and (optionally) integration with anthropic's
    tool_choice signaling.
    """
    refuse_set = set(refuse_on)

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        def wrapper(*args: Any, **kw: Any) -> str:
            task = str(args[0]) if args else kw.get("task", "")
            verdict = charter_preflight_inline(
                principal_id=principal_id,
                agent_id=agent_id,
                task=task,
                grader=grader,
            )
            if verdict["decision"] in refuse_set:
                applied = [m["id"] for m in verdict.get("matched_clauses", []) if m["applied"]]
                return (
                    f"Charter check returned {verdict['decision']}. "
                    f"Applied clauses: {applied}. {verdict.get('reason', '')}"
                )
            return fn(*args, **kw)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Build a Charter
# ---------------------------------------------------------------------------


def _build_charter() -> tuple[str, str]:
    from charter.schema import (
        AgentOperator,
        Binding,
        Charter,
        Clause,
        Issuer,
        Lifecycle,
        Principal,
        Provenance,
        Summary,
    )
    from charter.signing import public_key_to_string, sign_charter
    from charter.storage import ensure_issuer_key, save_charter

    now = datetime.now(UTC).replace(microsecond=0)
    private_key = ensure_issuer_key("alice@acme.com")
    charter = Charter(
        charter_id=f"charter:alice@acme.com:research_agent_v1:{now.date().isoformat()}",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Senior Accountant"),
        issuer=Issuer(id="alice@acme.com", relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(plain_language="Accounting work allowed; code authoring forbidden."),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting, tax, and bookkeeping work"),
            Clause(id="C-002", type="out_of_scope", text="Code authoring, UI design"),
        ],
        lifecycle=Lifecycle(
            issued_at=now, valid_until=now + timedelta(days=30), status="active"
        ),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(private_key.public_key()),
            issuer_signature="",
            generated_at=now,
        ),
    )
    sign_charter(charter, private_key)
    save_charter(charter)
    return charter.binding.principal_id, charter.binding.agent_id


def grade_task(charter: Any, task: str) -> list[dict[str, Any]]:
    t = task.lower()
    hits: list[dict[str, Any]] = []
    if "reconcile" in t or "tax" in t or "invoice" in t:
        hits.append(
            {"id": "C-001", "hit": True, "confidence": 0.92, "reason": "Accounting scope."}
        )
    if "react" in t or "component" in t or "code" in t:
        hits.append(
            {"id": "C-002", "hit": True, "confidence": 0.95, "reason": "Code authoring excluded."}
        )
    return hits


# ---------------------------------------------------------------------------
# Tool loop — mirrors the shape of a real anthropic tool-use agent loop.
# ---------------------------------------------------------------------------


def main() -> int:
    scratch = _ROOT / "data" / "cookbook_06"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)

    print("=" * 72)
    print("Cookbook #06 — Charter-gate Anthropic / Claude Agent SDK calls")
    print("=" * 72)
    print()

    principal_id, agent_id = _build_charter()
    print(f"Charter on disk for {principal_id} x {agent_id}.")
    print()

    # The two tools the "agent" can call. Both wrapped with the gate.
    @charter_gated_tool(principal_id=principal_id, agent_id=agent_id, grader=grade_task)
    def reconcile(task: str) -> str:
        return f"[accounting] reconciled: {task}"

    @charter_gated_tool(principal_id=principal_id, agent_id=agent_id, grader=grade_task)
    def write_code(task: str) -> str:
        return f"[code] wrote: {task}"

    tool_table = {"reconcile": reconcile, "write_code": write_code}

    # Fake an Anthropic agent that wants to call both tools.
    client = FakeAnthropic(
        tool_use_calls=[
            {"name": "reconcile", "input": {"task": "Reconcile Q1 invoices for tax prep."}},
            {"name": "write_code", "input": {"task": "Write a React component for the table."}},
        ]
    )

    # Tool-use loop. Real shape: while resp.stop_reason == "tool_use": ...
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Run two tasks."}]
    for step in range(2):
        resp = client.messages_create(model="claude-sonnet-4-6", messages=messages)
        for block in resp.content:
            if block.type == "tool_use":
                tool_fn = tool_table[block.name]
                tool_input = block.input
                result = tool_fn(tool_input["task"])
                print(f"step={step} tool={block.name!r}")
                print(f"  input:  {tool_input!r}")
                print(f"  output: {result!r}")
                print()
                # Real loop: feed `result` back as a tool_result block.
                messages.append({"role": "tool", "tool_use_id": block.id, "content": result})

    # Assertions: reconcile ran the body, write_code returned a refusal.
    # Walk the tool-result messages in order (first => reconcile, second
    # => write_code) since both fakes always append exactly one per turn.
    tool_results = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_results) == 2, f"Expected 2 tool results, got {len(tool_results)}."
    reconcile_result, write_code_result = tool_results
    assert "reconciled" in reconcile_result["content"], (
        f"Expected reconcile body to run. Got: {reconcile_result['content']!r}"
    )
    assert "Charter check returned" in write_code_result["content"], (
        f"Expected write_code to be refused. Got: {write_code_result['content']!r}"
    )

    print("[OK] Gate routed an in-scope tool through and refused an out-of-scope tool.")
    print()
    print("This is the same shape charter.adapters.anthropic.charter_gated will ship as")
    print("once ADR-012 promotes Anthropic adapter from deferred to active.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
