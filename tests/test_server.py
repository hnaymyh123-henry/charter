"""Integration tests for `charter.server` FastAPI host.

Uses `httpx.AsyncClient` + `ASGITransport` so we don't need to actually
bind a port. Each test points `CHARTER_DATA_DIR` at a temp directory so the
tests don't see your real Charters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

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
from charter.signing import generate_keypair, public_key_to_string, sign_charter
from charter.storage import save_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CHARTER_DATA_DIR at a temp dir so tests don't touch real data."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(temp_data_dir: Path):  # noqa: ARG001
    """Provide an httpx.AsyncClient bound to the FastAPI app via ASGI."""
    from charter.server import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_signed_charter(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    status: str = "active",
) -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[
            Clause(id="C-001", type="scope", text="..."),
            Clause(id="C-002", type="out_of_scope", text="..."),
        ],
        lifecycle=Lifecycle(
            issued_at=now,
            valid_until=now + timedelta(days=30),
            status=status,  # type: ignore[arg-type]
        ),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="test",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(charter, private)
    return charter


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Root index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_empty(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["charters"] == []
    assert body["self_hosted_principal"] is None


@pytest.mark.asyncio
async def test_root_lists_saved_charter(client: AsyncClient) -> None:
    save_charter(_make_signed_charter())
    async with client as ac:
        r = await ac.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["charters"][0]["principal_id"] == "alice@acme.com"


# ---------------------------------------------------------------------------
# GET /{principal_id}/{agent_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_charter_happy_path(client: AsyncClient) -> None:
    charter = _make_signed_charter()
    save_charter(charter)
    async with client as ac:
        r = await ac.get("/alice@acme.com/research_agent_v1")
    assert r.status_code == 200
    body = r.json()
    assert body["charter_id"] == charter.charter_id
    assert len(body["clauses"]) == 2


@pytest.mark.asyncio
async def test_get_charter_404_when_unknown(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/nobody@nowhere.io/missing_agent")
    assert r.status_code == 404
    assert r.json()["detail"] == "charter not found"


@pytest.mark.asyncio
async def test_get_charter_serves_expired_status(client: AsyncClient) -> None:
    """Server returns expired Charters as-is; lifecycle judgment happens
    in the calling agent (per the spec, the server is dumb storage)."""
    save_charter(_make_signed_charter(status="expired"))
    async with client as ac:
        r = await ac.get("/alice@acme.com/research_agent_v1")
    assert r.status_code == 200
    assert r.json()["lifecycle"]["status"] == "expired"


# ---------------------------------------------------------------------------
# /api/lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_resolves_known_binding(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://charter.example.com")
    save_charter(_make_signed_charter())
    async with client as ac:
        r = await ac.get(
            "/api/lookup",
            params={"principal_id": "alice@acme.com", "agent_id": "research_agent_v1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["charter_url"] == ("https://charter.example.com/alice@acme.com/research_agent_v1")


@pytest.mark.asyncio
async def test_lookup_404_when_unknown(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get(
            "/api/lookup",
            params={"principal_id": "x", "agent_id": "y"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /.well-known/charter/{agent_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_well_known_404_when_not_self_hosted(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/.well-known/charter/research_agent_v1")
    assert r.status_code == 404
    assert "self-hosted" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_well_known_resolves_when_self_hosted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_SELF_HOSTED_PRINCIPAL", "alice@acme.com")
    charter = _make_signed_charter()
    save_charter(charter)
    async with client as ac:
        r = await ac.get("/.well-known/charter/research_agent_v1")
    assert r.status_code == 200
    assert r.json()["charter_id"] == charter.charter_id


@pytest.mark.asyncio
async def test_well_known_404_when_agent_unknown(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_SELF_HOSTED_PRINCIPAL", "alice@acme.com")
    async with client as ac:
        r = await ac.get("/.well-known/charter/never_existed_agent")
    assert r.status_code == 404
    assert r.json()["detail"] == "charter not found"
