"""Cookbook #05 — Integrate OpenAI Agents SDK.

Runnable in two layers:

  Layer 1 (always runs, no extra deps):
      Exercises the `charter_preflight(...)` helper and the
      `charter_gated(...)` decorator from
      `charter.adapters.openai_agents` directly. Demonstrates that:
        - an in-scope task -> verdict `allow` -> decorated tool body runs
        - an out-of-scope task -> verdict `incompatible` -> decorated
          tool body is skipped and the wrapper returns a refusal string

  Layer 2 (skipped unless `openai-agents` is installed):
      The exact OpenAI Agents SDK runner from
      `examples/openai_agents_demo.py`. Importing `agents` is wrapped in
      try/except so the cookbook still passes when the optional SDK
      isn't available.

A hand-coded grader avoids the live Anthropic call so this works
offline.

Run from the repo root:

    python examples/cookbook/05-integrate-openai-agents-sdk/main.py
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _build_charter() -> tuple[str, str]:
    """Issue Alice's Charter via the offline seed and return ids."""
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
        summary=Summary(
            plain_language="Accounting work allowed; code authoring and marketing forbidden."
        ),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting, tax, bookkeeping work"),
            Clause(
                id="C-002",
                type="out_of_scope",
                text="Code authoring, marketing copy, UI design",
            ),
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
    """Substring-matching stand-in for a real LLM grader.

    Real usage: pass `charter.loopback._grade_via_llm` (default — needs
    ANTHROPIC_API_KEY) or your own OpenAI-backed grader to
    `charter_preflight(..., hits_grader=...)`.
    """
    t = task.lower()
    hits: list[dict[str, Any]] = []
    if any(w in t for w in ["reconcile", "tax", "invoice", "accounting", "bookkeeping"]):
        hits.append(
            {
                "id": "C-001",
                "hit": True,
                "confidence": 0.93,
                "reason": "Task matches accounting scope.",
            }
        )
    if any(w in t for w in ["react", "ui", "component", "marketing", "code"]):
        hits.append(
            {
                "id": "C-002",
                "hit": True,
                "confidence": 0.95,
                "reason": "Task matches code authoring / marketing / UI exclusion.",
            }
        )
    return hits


def _start_local_server() -> tuple[str, Any]:
    """Bring up charter.server.app on an ephemeral port in a thread."""
    import socket

    import uvicorn

    from charter.server import app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("local charter-server did not start within 2.5s")
    return f"http://127.0.0.1:{port}", server


def main() -> int:
    scratch = _ROOT / "data" / "cookbook_05"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)

    print("=" * 72)
    print("Cookbook #05 — Integrate OpenAI Agents SDK")
    print("=" * 72)
    print()

    principal_id, agent_id = _build_charter()
    base, server = _start_local_server()
    os.environ["CHARTER_URL_BASE"] = base
    charter_url = f"{base}/{principal_id}/{agent_id}"
    print(f"Charter published at: {charter_url}")
    print()

    try:
        # ---- Layer 1: preflight + decorator without the SDK -------------
        from charter.adapters.openai_agents import charter_gated, charter_preflight

        print("[layer 1] charter_preflight directly:")
        verdict_a = charter_preflight(
            charter_url,
            "Reconcile Q1 invoices for tax prep.",
            hits_grader=grade_task,
        )
        print(
            f"  task='Reconcile Q1 invoices'  -> "
            f"{verdict_a.decision}"
        )
        verdict_b = charter_preflight(
            charter_url,
            "Write a React component for the pricing table.",
            hits_grader=grade_task,
        )
        print(
            f"  task='Write a React component'  -> "
            f"{verdict_b.decision}"
        )
        assert verdict_a.decision == "allow"
        assert verdict_b.decision == "incompatible"
        print()

        print("[layer 1] charter_gated decorator:")

        @charter_gated(charter_url, hits_grader=grade_task)
        def accounting_tool(task: str) -> str:
            return f"[accounting] done: {task}"

        @charter_gated(charter_url, hits_grader=grade_task)
        def code_tool(task: str) -> str:
            return f"[code] wrote: {task}"

        result_a = accounting_tool("Reconcile Q1 invoices for tax prep.")
        result_b = code_tool("Write a React component for the pricing table.")
        print(f"  accounting_tool result: {result_a!r}")
        print(f"  code_tool result:       {result_b!r}")

        assert "done" in result_a, "Allowed task should have run the body."
        assert "Charter check returned" in result_b, (
            "Out-of-scope task should have been refused by the wrapper."
        )
        print()

        # ---- Layer 2: optional, only runs if openai-agents installed ----
        try:
            import agents  # noqa: F401, type: ignore[import-not-found]

            print("[layer 2] openai-agents is installed; running end-to-end demo.")
            sys.path.insert(0, str(_ROOT / "examples"))
            import openai_agents_demo  # type: ignore[import-not-found]

            openai_agents_demo.main()
        except ImportError:
            print(
                "[layer 2] skipped: openai-agents not installed. "
                "`pip install -e .[openai_agents]` to enable."
            )
        print()

        print("[OK] charter_preflight allowed scope tasks and refused out-of-scope tasks.")
        return 0
    finally:
        server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
