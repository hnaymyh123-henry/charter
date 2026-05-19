"""Tests for v0.8 #5: transparency-log HTTP endpoints.

Covers:
  - `GET /transparency/head` empty-log shape vs populated.
  - `GET /transparency/log` returns NDJSON.
  - `GET /transparency/log?since=N` skips entries with seq <= N.
  - `GET /transparency/proof/{charter_id}` returns entry + chain.
  - Proof chain ends exactly at target.seq and contains every prior entry_hash.
  - Proof 404s for an unknown charter_id.
  - Charter ids that contain colons resolve via the `:path` converter.
  - `sign_charter` populates `provenance.transparency_log_id`.
  - Mutating `transparency_log_id` post-sign does NOT break verify_charter
    (the field is excluded from the canonical bytes).
  - The transparency routes don't collide with the catch-all
    `/{principal_id}/{agent_id}` route.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from charter import transparency
from charter.schema import (
    AgentOperator,
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
from charter.signing import (
    generate_keypair,
    public_key_to_string,
    sign_charter,
    verify_charter,
)
from charter.storage import save_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))
    return tmp_path


@pytest.fixture
def client(temp_data_dir: Path):  # noqa: ARG001
    """ASGI client for the FastAPI app."""
    from charter.server import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_charter(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    seed: str = "",
) -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    suffix = f":{seed}" if seed else ""
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}{suffix}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[Clause(id="C-001", type="scope", text="...")],
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
    )
    sign_charter(charter, private)
    return charter


# ---------------------------------------------------------------------------
# transparency_log_id wire-in
# ---------------------------------------------------------------------------


def test_sign_charter_populates_transparency_log_id(temp_data_dir: Path) -> None:
    c = _make_charter()
    assert c.provenance.transparency_log_id == 1


def test_transparency_log_id_matches_log_seq(temp_data_dir: Path) -> None:
    c1 = _make_charter(seed="a")
    c2 = _make_charter(seed="b")
    assert c1.provenance.transparency_log_id == 1
    assert c2.provenance.transparency_log_id == 2

    # Sanity-check the log itself agrees.
    entry1 = transparency.get_entry(c1.charter_id)
    entry2 = transparency.get_entry(c2.charter_id)
    assert entry1 is not None and entry1.seq == 1
    assert entry2 is not None and entry2.seq == 2


def test_transparency_log_id_not_in_signed_payload(temp_data_dir: Path) -> None:
    """The field is intentionally excluded from the canonical bytes — a
    post-sign edit must NOT invalidate verify_charter."""
    c = _make_charter()
    assert verify_charter(c) is True
    c.provenance.transparency_log_id = 9999  # post-sign tamper
    assert verify_charter(c) is True


# ---------------------------------------------------------------------------
# GET /transparency/head
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_head_empty_log(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/transparency/head")
    assert r.status_code == 200
    body = r.json()
    assert body["seq"] == 0
    assert body["entry_hash"] == transparency.GENESIS_PREV_HASH
    assert body["appended_at"] is None


@pytest.mark.asyncio
async def test_head_returns_last_entry(client: AsyncClient) -> None:
    _make_charter(seed="a")
    last = _make_charter(seed="b")

    async with client as ac:
        r = await ac.get("/transparency/head")
    assert r.status_code == 200
    body = r.json()
    assert body["seq"] == last.provenance.transparency_log_id
    assert body["entry_hash"].startswith("sha256:")
    assert body["appended_at"] is not None


# ---------------------------------------------------------------------------
# GET /transparency/log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_returns_ndjson(client: AsyncClient) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")
    _make_charter(seed="c")

    async with client as ac:
        r = await ac.get("/transparency/log")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 3
    entries = [json.loads(ln) for ln in lines]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    # entry shape matches the writer's contract
    assert set(entries[0].keys()) == {
        "seq",
        "charter_id",
        "binding",
        "issuer_kid",
        "issuer_signature",
        "appended_at",
        "prev_hash",
        "entry_hash",
    }


@pytest.mark.asyncio
async def test_log_since_filter(client: AsyncClient) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")
    _make_charter(seed="c")

    async with client as ac:
        r = await ac.get("/transparency/log", params={"since": 2})
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 3


@pytest.mark.asyncio
async def test_log_since_beyond_head_returns_empty(client: AsyncClient) -> None:
    _make_charter(seed="a")

    async with client as ac:
        r = await ac.get("/transparency/log", params={"since": 99})
    assert r.status_code == 200
    assert r.text.strip() == ""


@pytest.mark.asyncio
async def test_log_empty_returns_empty_body(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/transparency/log")
    assert r.status_code == 200
    assert r.text.strip() == ""


# ---------------------------------------------------------------------------
# GET /transparency/proof/{charter_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proof_returns_entry_and_chain(client: AsyncClient) -> None:
    _make_charter(seed="a")
    c2 = _make_charter(seed="b")
    _make_charter(seed="c")

    async with client as ac:
        r = await ac.get(f"/transparency/proof/{c2.charter_id}")
    assert r.status_code == 200
    body = r.json()

    # The entry matches what /transparency/log shows for seq=2.
    assert body["entry"]["charter_id"] == c2.charter_id
    assert body["entry"]["seq"] == 2

    # Chain is every (seq, entry_hash) pair from 1 up to and including target.
    seqs = [step["seq"] for step in body["chain"]]
    assert seqs == [1, 2]
    # The chain does NOT include seq=3 (after the target).
    assert all(step["seq"] != 3 for step in body["chain"])
    # Each step has only seq + entry_hash, no extra fields.
    assert all(set(step.keys()) == {"seq", "entry_hash"} for step in body["chain"])


@pytest.mark.asyncio
async def test_proof_404s_unknown_charter(client: AsyncClient) -> None:
    _make_charter()
    async with client as ac:
        r = await ac.get("/transparency/proof/charter:nobody:agent:2026-01-01")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_proof_handles_colons_in_charter_id(client: AsyncClient) -> None:
    """Canonical charter_ids contain colons. The route uses `:path` so
    FastAPI doesn't reject them as multi-segment paths."""
    c = _make_charter()
    assert ":" in c.charter_id

    async with client as ac:
        r = await ac.get(f"/transparency/proof/{c.charter_id}")
    assert r.status_code == 200
    assert r.json()["entry"]["charter_id"] == c.charter_id


# ---------------------------------------------------------------------------
# Route precedence — transparency must not collide with /{principal}/{agent}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transparency_routes_take_precedence_over_catchall(
    client: AsyncClient,
) -> None:
    """If the catch-all `/{principal_id}/{agent_id}` route swallowed
    `/transparency/log`, this would 404 (no Charter at the bogus binding).
    A 200 NDJSON response proves precedence works."""
    async with client as ac:
        r = await ac.get("/transparency/log")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")


@pytest.mark.asyncio
async def test_catchall_still_serves_real_charters(
    client: AsyncClient,
) -> None:
    """Sanity check the catch-all route still works post-routing changes."""
    c = _make_charter()
    save_charter(c)

    async with client as ac:
        r = await ac.get(f"/{c.binding.principal_id}/{c.binding.agent_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["charter_id"] == c.charter_id
