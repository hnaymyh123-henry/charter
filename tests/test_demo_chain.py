"""Tests for scripts/demo_chain.py — exercises the full two-hop chain.

We exercise the script's library entry points (`seed_chain`, the hit
fixtures, the tool callers) rather than spawning a subprocess; that
makes the test fast and lets us assert on individual verdicts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `scripts/` importable as a package-less directory.
_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import demo_chain  # noqa: E402

from charter.chain import verify_chain  # noqa: E402
from charter.mcp_server import (  # noqa: E402
    aggregate_verdict_chain,
    fetch_charter_chain,
)
from charter.storage import load_charter  # noqa: E402


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


def _call(tool, *args, **kwargs):
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_chain_writes_both_charters(temp_data_dir, monkeypatch):
    monkeypatch.setenv("CHARTER_URL_BASE", "http://test:8000")
    charter_a, charter_b = demo_chain.seed_chain(base_url="http://test:8000")

    # Both on disk under their bindings.
    loaded_a = load_charter("acme_corp", "assistant_agent_v1")
    loaded_b = load_charter("assistant_agent_v1", "research_agent_v1")
    assert loaded_a is not None and loaded_a.charter_id == charter_a.charter_id
    assert loaded_b is not None and loaded_b.charter_id == charter_b.charter_id


def test_child_charter_references_parent(temp_data_dir, monkeypatch):
    monkeypatch.setenv("CHARTER_URL_BASE", "http://test:8000")
    charter_a, charter_b = demo_chain.seed_chain(base_url="http://test:8000")

    assert charter_b.parent_charter_url == "http://test:8000/acme_corp/assistant_agent_v1"
    assert charter_b.attenuation_proof is not None
    assert charter_b.attenuation_proof.parent_charter_id == charter_a.charter_id


def test_chain_passes_verify_chain(temp_data_dir, monkeypatch):
    """The child Charter must actually be a strict subset of the parent —
    confirms the demo data is consistent with the verify_chain rules."""
    monkeypatch.setenv("CHARTER_URL_BASE", "http://test:8000")
    charter_a, charter_b = demo_chain.seed_chain(base_url="http://test:8000")
    assert verify_chain(charter_b, charter_a) is True


# ---------------------------------------------------------------------------
# Tool integration — stub `_fetch_and_verify` so we don't need uvicorn here
# ---------------------------------------------------------------------------


def test_fetch_charter_chain_returns_two_hops(temp_data_dir, monkeypatch):
    monkeypatch.setenv("CHARTER_URL_BASE", "http://test:8000")
    charter_a, charter_b = demo_chain.seed_chain(base_url="http://test:8000")

    url_to_charter = {
        "http://test:8000/acme_corp/assistant_agent_v1": charter_a,
        "http://test:8000/assistant_agent_v1/research_agent_v1": charter_b,
    }
    monkeypatch.setattr(
        "charter.mcp_server._fetch_and_verify",
        lambda url: url_to_charter[url],
    )

    result = _call(
        fetch_charter_chain,
        "http://test:8000/assistant_agent_v1/research_agent_v1",
    )
    assert result["ok"] is True
    assert result["depth"] == 2
    assert [c["charter_id"] for c in result["chain"]] == [
        charter_a.charter_id,
        charter_b.charter_id,
    ]


# ---------------------------------------------------------------------------
# Three sample tasks → expected chain-wide verdicts
# ---------------------------------------------------------------------------


def _setup_chain(monkeypatch):
    """Helper: seed chain + stub fetch + return chain + charters."""
    monkeypatch.setenv("CHARTER_URL_BASE", "http://test:8000")
    charter_a, charter_b = demo_chain.seed_chain(base_url="http://test:8000")
    url_to_charter = {
        "http://test:8000/acme_corp/assistant_agent_v1": charter_a,
        "http://test:8000/assistant_agent_v1/research_agent_v1": charter_b,
    }
    monkeypatch.setattr(
        "charter.mcp_server._fetch_and_verify",
        lambda url: url_to_charter[url],
    )
    result = _call(
        fetch_charter_chain,
        "http://test:8000/assistant_agent_v1/research_agent_v1",
    )
    return result["chain"], charter_a, charter_b


def test_reconcile_invoices_is_allowed_across_chain(temp_data_dir, monkeypatch):
    chain, a, b = _setup_chain(monkeypatch)
    hits = demo_chain._build_hits(a, b)["Reconcile Q1 invoices"]
    verdict = _call(aggregate_verdict_chain, chain, hits)
    assert verdict["decision"] == "allow"


def test_marketing_landing_page_is_incompatible(temp_data_dir, monkeypatch):
    chain, a, b = _setup_chain(monkeypatch)
    hits = demo_chain._build_hits(a, b)["Write a marketing landing page"]
    verdict = _call(aggregate_verdict_chain, chain, hits)
    assert verdict["decision"] == "incompatible"
    applied = [m for m in verdict["matched_clauses"] if m["applied"]]
    # Either Charter A or B (or both) could be applied — both hit C-101.
    applied_sources = {m["source_charter_id"] for m in applied}
    assert applied_sources <= {a.charter_id, b.charter_id}


def test_pii_export_caught_by_child_only(temp_data_dir, monkeypatch):
    """The defining test for the demo: A doesn't catch this restriction;
    only B does. The chain enforces the UNION of restrictions."""
    chain, a, b = _setup_chain(monkeypatch)
    hits = demo_chain._build_hits(a, b)["Export customer PII to CSV"]
    verdict = _call(aggregate_verdict_chain, chain, hits)

    assert verdict["decision"] == "incompatible"
    applied = [m for m in verdict["matched_clauses"] if m["applied"]]
    assert len(applied) == 1
    # The applied clause MUST come from the child (B), not the parent (A).
    assert applied[0]["source_charter_id"] == b.charter_id
    assert applied[0]["id"] == "C-103"
