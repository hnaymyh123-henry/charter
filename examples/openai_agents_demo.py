"""Minimal OpenAI Agents SDK example with a Charter-gated tool.

Run after `pip install -e .[openai_agents]`:

    python examples/openai_agents_demo.py

The example:

  1. Issues a single Alice Charter (no LLM call — uses the seed-demo
     hand-curated clauses). The Charter excludes code authoring.
  2. Defines two tools, both wrapped with @charter_gated:
       - `accounting_task(...)` — work that falls in C-001 scope
       - `code_authoring_task(...)` — work that hits C-002 out_of_scope
  3. Runs the agent twice, once per tool, and prints the result.

Expected output:
  - accounting_task: runs normally.
  - code_authoring_task: tool body is skipped; the wrapper returns a
    refusal string with the applied clause ID.

This is a 60-LOC example, not a polished demo. The point is to show
that wrapping a real framework's tool with `charter_gated` is one line.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _ensure_alice_charter_on_disk() -> str:
    """Seed Alice's Charter into ./data and return its charter_url."""
    import scripts.seed_demo as seed_demo  # noqa: PLC0415

    profile_path = _ROOT / "profiles" / "alice.yaml"
    if not profile_path.exists():
        raise FileNotFoundError(profile_path)
    seed_demo.seed_from_profile(profile_path)

    import os

    base = os.environ.get("CHARTER_URL_BASE", "http://localhost:8000").rstrip("/")
    return f"{base}/alice@acme.com/research_agent_v1"


def main() -> int:
    try:
        from agents import Agent, Runner, function_tool  # type: ignore[import-not-found]
    except ImportError:
        print(
            "openai-agents is not installed. Run:\n  pip install -e '.[openai_agents]'",
            file=sys.stderr,
        )
        return 1

    from charter.adapters.openai_agents import charter_gated

    charter_url = _ensure_alice_charter_on_disk()
    print(f"Alice Charter is published at {charter_url}")
    print("Make sure `charter-server` is running before this script.\n")

    @charter_gated(charter_url)
    @function_tool
    def accounting_task(task: str) -> str:
        return f"[accounting] done: {task}"

    @charter_gated(charter_url)
    @function_tool
    def code_authoring_task(task: str) -> str:
        return f"[code] wrote: {task}"

    agent = Agent(
        name="charter-gated demo agent",
        instructions=(
            "You have two tools. If a task is about accounting, call "
            "accounting_task(task). If a task is about writing code, "
            "call code_authoring_task(task)."
        ),
        tools=[accounting_task, code_authoring_task],
    )

    for task in [
        "Reconcile Q1 invoices for tax prep.",
        "Write a React component that displays a pricing table.",
    ]:
        print(f"--- task: {task!r}")
        result = Runner.run_sync(agent, task)
        print(result.final_output)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
