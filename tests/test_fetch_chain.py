"""Tests for the `fetch_charter_chain` MCP tool.

We stub `_fetch_and_verify` with a URL -> Charter dict so each test
controls exactly which hop returns what without going through HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from charter.mcp_server import fetch_charter_chain as _tool
from charter.schema import (
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
from charter.signing import generate_keypair, public_key_to_string, sign_charter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed(
    *,
    charter_id: str,
    parent_charter_url: str | None = None,
    parent_charter_id: str | None = None,
    clauses: list[Clause] | None = None,
) -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    proof = (
        AttenuationProof(parent_charter_id=parent_charter_id)
        if parent_charter_id is not None
        else None
    )
    c = Charter(
        charter_id=charter_id,
        binding=Binding(principal_id="p", agent_id="a"),
        principal=Principal(id="p", role_summary="Test"),
        issuer=Issuer(id="p"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=clauses or [Clause(id="C-001", type="scope", text="anything")],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="t",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
        parent_charter_url=parent_charter_url,
        attenuation_proof=proof,
    )
    sign_charter(c, private)
    return c


def _call(tool: Any, *args: Any, **kwargs: Any) -> Any:
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)


@pytest.fixture
def url_to_charter(monkeypatch: pytest.MonkeyPatch):
    """Returns a dict the test can populate with url -> Charter mappings.

    Replaces `_fetch_and_verify` so each test controls fetches.
    """
    store: dict[str, Charter] = {}

    def fake_fetch(url: str) -> Charter:
        if url not in store:
            from charter.errors import CharterNotFoundError

            raise CharterNotFoundError(f"GET {url} -> 404")
        return store[url]

    monkeypatch.setattr("charter.mcp_server._fetch_and_verify", fake_fetch)
    return store


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_single_charter_no_parent_returns_chain_of_one(url_to_charter) -> None:
    leaf = _signed(charter_id="leaf:1")
    url_to_charter["http://test/leaf"] = leaf

    result = _call(_tool, "http://test/leaf")

    assert result["ok"] is True
    assert result["depth"] == 1
    assert result["chain"][0]["charter_id"] == "leaf:1"


def test_two_hop_chain_returns_root_first(url_to_charter) -> None:
    parent = _signed(
        charter_id="parent:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    child = _signed(
        charter_id="child:1",
        parent_charter_url="http://test/parent",
        parent_charter_id="parent:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    url_to_charter["http://test/parent"] = parent
    url_to_charter["http://test/child"] = child

    result = _call(_tool, "http://test/child")

    assert result["ok"] is True
    assert result["depth"] == 2
    assert result["chain"][0]["charter_id"] == "parent:1"
    assert result["chain"][1]["charter_id"] == "child:1"


def test_three_hop_chain_works(url_to_charter) -> None:
    root = _signed(
        charter_id="root:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    mid = _signed(
        charter_id="mid:1",
        parent_charter_url="http://test/root",
        parent_charter_id="root:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    leaf = _signed(
        charter_id="leaf:1",
        parent_charter_url="http://test/mid",
        parent_charter_id="mid:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    url_to_charter["http://test/root"] = root
    url_to_charter["http://test/mid"] = mid
    url_to_charter["http://test/leaf"] = leaf

    result = _call(_tool, "http://test/leaf")

    assert result["ok"] is True
    assert result["depth"] == 3
    assert [c["charter_id"] for c in result["chain"]] == ["root:1", "mid:1", "leaf:1"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_attenuation_broken_returns_partial_chain(url_to_charter) -> None:
    """Child relaxes parent's out_of_scope — chain rejected at that hop."""
    parent = _signed(
        charter_id="parent:1",
        clauses=[
            Clause(id="C-001", type="scope", text="X"),
            Clause(id="C-002", type="out_of_scope", text="Do not write marketing."),
        ],
    )
    child = _signed(
        charter_id="child:1",
        parent_charter_url="http://test/parent",
        parent_charter_id="parent:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],  # missing out_of_scope
    )
    url_to_charter["http://test/parent"] = parent
    url_to_charter["http://test/child"] = child

    result = _call(_tool, "http://test/child")

    assert result["ok"] is False
    assert "attenuation broken" in result["reason"]
    assert "child:1" in result["reason"] and "parent:1" in result["reason"]
    # Both Charters fetched but verification failed; partial includes both.
    assert [c["charter_id"] for c in result["partial"]] == ["parent:1", "child:1"]


def test_max_depth_exceeded_returns_failure(url_to_charter) -> None:
    """A 3-hop chain with max_depth=2 should fail mid-walk."""
    root = _signed(charter_id="root:1", clauses=[Clause(id="C-001", type="scope", text="X")])
    mid = _signed(
        charter_id="mid:1",
        parent_charter_url="http://test/root",
        parent_charter_id="root:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    leaf = _signed(
        charter_id="leaf:1",
        parent_charter_url="http://test/mid",
        parent_charter_id="mid:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    url_to_charter["http://test/root"] = root
    url_to_charter["http://test/mid"] = mid
    url_to_charter["http://test/leaf"] = leaf

    result = _call(_tool, "http://test/leaf", max_depth=2)

    assert result["ok"] is False
    assert "max_depth=2 exceeded" in result["reason"]


def test_invalid_max_depth_rejected(url_to_charter) -> None:
    result = _call(_tool, "http://test/x", max_depth=0)
    assert result["ok"] is False
    assert "max_depth" in result["reason"]


def test_cycle_detected(url_to_charter) -> None:
    """A points at B, B points back at A — must not loop forever."""
    a = _signed(
        charter_id="A",
        parent_charter_url="http://test/b",
        parent_charter_id="B",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    b = _signed(
        charter_id="B",
        parent_charter_url="http://test/a",
        parent_charter_id="A",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    url_to_charter["http://test/a"] = a
    url_to_charter["http://test/b"] = b

    result = _call(_tool, "http://test/a")

    assert result["ok"] is False
    assert "cycle detected" in result["reason"]


def test_fetch_failure_propagates_as_chain_failure(url_to_charter) -> None:
    """If a hop's fetch raises, the tool returns ok=false rather than
    bubbling the exception up to the MCP transport."""
    parent_does_not_exist = "http://test/parent-404"
    child = _signed(
        charter_id="child:1",
        parent_charter_url=parent_does_not_exist,
        parent_charter_id="parent:1",
        clauses=[Clause(id="C-001", type="scope", text="X")],
    )
    url_to_charter["http://test/child"] = child
    # url_to_charter does NOT contain parent_does_not_exist

    result = _call(_tool, "http://test/child")

    assert result["ok"] is False
    assert "CharterNotFoundError" in result["reason"]
    # The child was successfully fetched before the parent failed.
    assert any(c["charter_id"] == "child:1" for c in result["partial"])
