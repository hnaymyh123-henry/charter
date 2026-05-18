"""End-to-end Charter Chain demo (no live LLM required).

Seeds two Charters that form an attenuation chain:

    acme_corp  ──Charter A──▶  assistant_agent_v1
                                │
                                │  (assistant acts as principal toward its sub-agent)
                                │
                                Charter B (subset of A)  ──▶  research_agent_v1

Then runs three sample tasks through fetch_charter_chain +
aggregate_verdict_chain and prints the chain-wide verdict for each.

Usage:
    python scripts/demo_chain.py

The script uses hand-curated clauses (same pattern as scripts/seed_demo.py)
so it runs without an ANTHROPIC_API_KEY. It writes Charters to
`data/charters/` and serves them via an in-process HTTP fixture so the
fetch_charter_chain tool can walk them like any real chain.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from charter.mcp_server import (  # noqa: E402
    aggregate_verdict_chain,
    fetch_charter_chain,
)
from charter.projection import _hash_profile, load_profile  # noqa: E402
from charter.schema import (  # noqa: E402
    AgentOperator,
    AttenuationProof,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    Provenance,
    SourceCommitment,
    Summary,
)
from charter.signing import public_key_to_string, sign_charter  # noqa: E402
from charter.storage import ensure_issuer_key, save_charter  # noqa: E402

# ---------------------------------------------------------------------------
# Hand-curated clauses for the two-hop chain
# ---------------------------------------------------------------------------

# Charter A (root) — Acme Corp -> assistant_agent_v1
A_CLAUSES = [
    Clause(id="C-001", type="scope", text="Engineering work"),
    Clause(id="C-002", type="scope", text="Accounting and tax work"),
    Clause(id="C-101", type="out_of_scope", text="Marketing copy"),
    Clause(id="C-102", type="out_of_scope", text="Cold-email campaigns"),
    Clause(
        id="C-201",
        type="approval_required",
        text="Any deployment to production environments",
    ),
    Clause(
        id="C-202",
        type="approval_required",
        text="Any destructive database action — DROP TABLE, DELETE, TRUNCATE, backup deletion",
    ),
]
A_SUMMARY = (
    "Charter for assistant_agent_v1 acting on behalf of Acme Corp. "
    "Engineering + accounting in scope; marketing copy and cold-email "
    "campaigns are excluded; production deploys and destructive DB "
    "actions need explicit approval."
)

# Charter B (child) — assistant_agent_v1 -> research_agent_v1.
# Must be a strict subset of Charter A:
#   - Scope narrowed: drops "Engineering work".
#   - All of A's out_of_scope preserved.
#   - One new out_of_scope added (assistant's own additional restriction).
#   - All of A's approval_required preserved verbatim.
B_CLAUSES = [
    Clause(id="C-002", type="scope", text="Accounting and tax work"),  # subset of A's scope
    Clause(id="C-101", type="out_of_scope", text="Marketing copy"),  # inherited
    Clause(id="C-102", type="out_of_scope", text="Cold-email campaigns"),  # inherited
    Clause(
        id="C-103",
        type="out_of_scope",
        text="Customer PII export to CSV",  # added by assistant
    ),
    Clause(
        id="C-201",
        type="approval_required",
        text="Any deployment to production environments",
    ),
    Clause(
        id="C-202",
        type="approval_required",
        text="Any destructive database action — DROP TABLE, DELETE, TRUNCATE, backup deletion",
    ),
]
B_SUMMARY = (
    "Charter for research_agent_v1 acting on behalf of assistant_agent_v1, "
    "which itself acts for Acme Corp. Narrower than the parent: accounting "
    "work only, plus an explicit ban on customer PII export."
)


# ---------------------------------------------------------------------------
# Build + sign the chain
# ---------------------------------------------------------------------------


def _build_charter(
    *,
    principal_id: str,
    agent_id: str,
    profile_path: Path,
    clauses: list[Clause],
    summary: str,
    parent_charter_url: str | None = None,
    parent_charter_id: str | None = None,
) -> Charter:
    profile, raw = load_profile(profile_path)
    private_key = ensure_issuer_key(principal_id)
    public_key_str = public_key_to_string(private_key.public_key())

    now = datetime.now(UTC).replace(microsecond=0)
    valid_until = now + timedelta(days=profile.lifecycle.valid_days)

    proof = (
        AttenuationProof(parent_charter_id=parent_charter_id)
        if parent_charter_id is not None
        else None
    )

    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary=profile.principal.role),
        issuer=Issuer(id=principal_id, relationship_to_principal="self"),
        agent_operator=AgentOperator(
            id="generic_worker_agent_provider",
            agent_card_url=profile.agent.card_url,
        ),
        summary=Summary(plain_language=summary),
        clauses=clauses,
        lifecycle=Lifecycle(issued_at=now, valid_until=valid_until, status="active"),
        provenance=Provenance(
            issuer_public_key=public_key_str,
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description=(
                        f"{principal_id} profile on {now.date().isoformat()} (seeded, no live LLM)"
                    ),
                    content_hash=_hash_profile(raw),
                )
            ],
            generated_at=now,
        ),
        parent_charter_url=parent_charter_url,
        attenuation_proof=proof,
    )
    sign_charter(charter, private_key)
    return charter


def seed_chain(*, base_url: str) -> tuple[Charter, Charter]:
    """Issue Charter A then Charter B, with B pointing at A as parent.

    Both Charters are saved to data/charters/. Returns (root, leaf).
    """
    profile_dir = _ROOT / "profiles"

    charter_a = _build_charter(
        principal_id="acme_corp",
        agent_id="assistant_agent_v1",
        profile_path=profile_dir / "acme_corp.yaml",
        clauses=A_CLAUSES,
        summary=A_SUMMARY,
    )
    save_charter(charter_a)
    a_url = f"{base_url}/acme_corp/assistant_agent_v1"

    charter_b = _build_charter(
        principal_id="assistant_agent_v1",
        agent_id="research_agent_v1",
        profile_path=profile_dir / "acme_assistant.yaml",
        clauses=B_CLAUSES,
        summary=B_SUMMARY,
        parent_charter_url=a_url,
        parent_charter_id=charter_a.charter_id,
    )
    save_charter(charter_b)

    return charter_a, charter_b


# ---------------------------------------------------------------------------
# Local HTTP fixture — serves the charter_server FastAPI app in-process
# ---------------------------------------------------------------------------


@contextmanager
def _live_server():
    """Run charter.server.app on a free localhost port in a daemon thread."""
    # Pick an open port by binding 0.
    import socket

    # FastAPI / Starlette apps speak ASGI, but for the demo's tiny needs we
    # can sidestep with a wsgiref bridge if we used Flask. We're on FastAPI,
    # so use uvicorn in a thread instead.
    import uvicorn

    from charter.server import app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to come up.
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("demo server did not start within 2.5s")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Hits — simulate what the calling agent's LLM would produce for each task
# ---------------------------------------------------------------------------


def _hits_reconcile_invoices() -> dict[str, list[dict[str, Any]]]:
    """In-scope on both Charters."""
    return {
        # Charter A: matches A's accounting scope C-002.
        # (Use the actual charter_ids; we look those up after seed_chain.)
    }


def _hits_marketing_landing_page() -> dict[str, list[dict[str, Any]]]:
    """Hits A's out_of_scope (Marketing copy). Should be incompatible at
    the chain level, applied clause sourced from A."""
    return {}


def _hits_pii_export() -> dict[str, list[dict[str, Any]]]:
    """Hits B's out_of_scope (Customer PII export). Parent (A) doesn't
    have this clause; only the child catches it. Demonstrates that the
    chain enforces the union of restrictions."""
    return {}


def _build_hits(charter_a: Charter, charter_b: Charter) -> dict[str, dict[str, list[dict]]]:
    """Build hit-maps for the three demo tasks, keyed by charter_id."""
    return {
        "Reconcile Q1 invoices": {
            charter_a.charter_id: [
                {
                    "id": "C-002",
                    "hit": True,
                    "confidence": 0.92,
                    "reason": "Falls in accounting scope.",
                }
            ],
            charter_b.charter_id: [
                {
                    "id": "C-002",
                    "hit": True,
                    "confidence": 0.94,
                    "reason": "Falls in narrower accounting scope.",
                }
            ],
        },
        "Write a marketing landing page": {
            charter_a.charter_id: [
                {
                    "id": "C-101",
                    "hit": True,
                    "confidence": 0.96,
                    "reason": "Explicit marketing copy task.",
                }
            ],
            charter_b.charter_id: [
                {
                    "id": "C-101",
                    "hit": True,
                    "confidence": 0.96,
                    "reason": "Explicit marketing copy task.",
                }
            ],
        },
        "Export customer PII to CSV": {
            # A does NOT have this restriction — by itself A would let it through.
            charter_a.charter_id: [],
            # B catches it; the child Charter is the one forcing the deny.
            charter_b.charter_id: [
                {
                    "id": "C-103",
                    "hit": True,
                    "confidence": 0.93,
                    "reason": "Task is explicit customer PII export.",
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# Tool callers — unwrap the @mcp.tool decorator
# ---------------------------------------------------------------------------


def _call_tool(tool: Any, *args: Any, **kwargs: Any) -> Any:
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


def _print_chain(chain: list[dict[str, Any]]) -> None:
    print()
    print("Charter Chain (root -> leaf):")
    for i, c in enumerate(chain):
        prefix = "  " * i + ("└─ " if i > 0 else "")
        principal_id = c.get("principal", {}).get("id", "?")
        agent_id = c.get("binding", {}).get("agent_id", "?")
        print(f"  {prefix}[{i + 1}] {principal_id} -> {agent_id}")
        print(f"  {'  ' * i}    charter_id: {c.get('charter_id')}")
        oos = [cl["text"] for cl in c.get("clauses", []) if cl["type"] == "out_of_scope"]
        if oos:
            print(f"  {'  ' * i}    out_of_scope: {oos}")
    print()


def _print_verdict(task: str, verdict: dict[str, Any]) -> None:
    decision = verdict.get("decision", "?").upper()
    applied = [
        f"{m.get('source_charter_id', '?')}::{m.get('id')}"
        for m in verdict.get("matched_clauses", [])
        if m.get("applied")
    ]
    print(f"Task: {task!r}")
    print(f"   -> {decision}")
    if applied:
        print(f"   applied: {applied}")
    print(f"   reason: {verdict.get('reason', '')}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 72)
    print("Charter Chain demo — Acme Corp -> assistant_agent_v1 -> research_agent_v1")
    print("=" * 72)

    with _live_server() as base_url:
        os.environ["CHARTER_URL_BASE"] = base_url

        charter_a, charter_b = seed_chain(base_url=base_url)
        leaf_url = f"{base_url}/assistant_agent_v1/research_agent_v1"

        result = _call_tool(fetch_charter_chain, leaf_url)
        if not result.get("ok"):
            print("fetch_charter_chain failed:", result)
            return 1

        chain = result["chain"]
        _print_chain(chain)

        hits_by_task = _build_hits(charter_a, charter_b)

        expected = {
            "Reconcile Q1 invoices": "allow",
            "Write a marketing landing page": "incompatible",
            "Export customer PII to CSV": "incompatible",
        }
        all_ok = True
        for task, hits in hits_by_task.items():
            verdict = _call_tool(aggregate_verdict_chain, chain, hits)
            _print_verdict(task, verdict)
            if verdict.get("decision") != expected[task]:
                all_ok = False
                print(
                    f"   !! expected {expected[task]} but got {verdict.get('decision')}",
                )

        return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
