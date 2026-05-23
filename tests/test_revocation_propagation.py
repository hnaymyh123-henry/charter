"""Tests for B1.3 revocation propagation.

Covers:
  - Cache-Control header on the 3 Charter-returning routes (and not on
    /healthz / /transparency/* / /.well-known/jwks.json).
  - `CHARTER_CACHE_TTL` env override.
  - `GET /transparency/revoked?since=0` returns every revoked Charter as
    NDJSON.
  - `since=N` filters to entries with seq > N only.
  - Non-revoked Charters do NOT appear in the feed.
  - Malformed `since` (negative / non-integer) returns 400.
  - `subscribe_revocations` async generator yields new entries while
    polling.
  - `RevocationAwareCache` auto-evicts cached entries when their
    charter_id arrives in the stream.
  - `_is_charter_response_path` / `_cache_ttl_seconds` pure helpers.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from charter import revocation
from charter.revocation import (
    RevocationAwareCache,
    RevocationEntry,
    subscribe_revocations,
)
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
from charter.server import _cache_ttl_seconds, _is_charter_response_path
from charter.signing import generate_keypair, public_key_to_string, sign_charter
from charter.storage import save_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CHARTER_DATA_DIR at a temp dir so tests don't touch real data."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))
    monkeypatch.delenv("CHARTER_CACHE_TTL", raising=False)
    monkeypatch.delenv("CHARTER_SELF_HOSTED_PRINCIPAL", raising=False)
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
    status: str = "active",
    revoked_at: datetime | None = None,
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
        lifecycle=Lifecycle(
            issued_at=now,
            valid_until=now + timedelta(days=30),
            status=status,  # type: ignore[arg-type]
            revoked_at=revoked_at,
        ),
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
    save_charter(charter)
    return charter


def _revoke_in_place(charter: Charter) -> Charter:
    """Mutate the saved Charter to look like `charter revoke` ran."""
    now = datetime.now(UTC).replace(microsecond=0)
    charter.lifecycle.status = "revoked"
    charter.lifecycle.revoked_at = now
    charter.provenance.issuer_signature = ""
    # Re-sign so the on-disk Charter is internally consistent. We need
    # the issuer's private key for that — generate a fresh keypair and
    # rewrite the public key so verify_charter keeps passing. The
    # transparency log append is idempotent on charter_id, so seq is
    # unchanged.
    private, public = generate_keypair()
    charter.provenance.issuer_public_key = public_key_to_string(public)
    charter.provenance.issuer_kid = None  # re-derived inside sign_charter
    sign_charter(charter, private)
    save_charter(charter)
    return charter


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------


def test_cache_ttl_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHARTER_CACHE_TTL", raising=False)
    assert _cache_ttl_seconds() == 300


def test_cache_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_CACHE_TTL", "60")
    assert _cache_ttl_seconds() == 60


def test_cache_ttl_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_CACHE_TTL", "0")
    assert _cache_ttl_seconds() == 0


def test_cache_ttl_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_CACHE_TTL", "not-a-number")
    assert _cache_ttl_seconds() == 300


@pytest.mark.parametrize(
    "path",
    [
        "/alice@acme.com/research_agent_v1",
        "/.well-known/charter/research_agent_v1",
        "/api/lookup",
    ],
)
def test_is_charter_response_path_matches(path: str) -> None:
    assert _is_charter_response_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/.well-known/jwks.json",
        "/transparency/head",
        "/transparency/log",
        "/transparency/proof/charter:x:y:2026-01-01",
        "/transparency/revoked",
        "/disclosures/foo/bar",
        "/",
    ],
)
def test_is_charter_response_path_excludes(path: str) -> None:
    assert _is_charter_response_path(path) is False


# ---------------------------------------------------------------------------
# Cache-Control header on Charter responses (AC #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_control_on_get_charter(client: AsyncClient) -> None:
    c = _make_charter()
    async with client as ac:
        r = await ac.get(f"/{c.binding.principal_id}/{c.binding.agent_id}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "max-age=300, must-revalidate"


@pytest.mark.asyncio
async def test_cache_control_on_well_known(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_SELF_HOSTED_PRINCIPAL", "alice@acme.com")
    _make_charter()
    async with client as ac:
        r = await ac.get("/.well-known/charter/research_agent_v1")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "max-age=300, must-revalidate"


@pytest.mark.asyncio
async def test_cache_control_on_api_lookup(client: AsyncClient) -> None:
    _make_charter()
    async with client as ac:
        r = await ac.get(
            "/api/lookup",
            params={"principal_id": "alice@acme.com", "agent_id": "research_agent_v1"},
        )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "max-age=300, must-revalidate"


@pytest.mark.asyncio
async def test_cache_control_env_override(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_CACHE_TTL", "60")
    c = _make_charter()
    async with client as ac:
        r = await ac.get(f"/{c.binding.principal_id}/{c.binding.agent_id}")
    assert r.headers["cache-control"] == "max-age=60, must-revalidate"


@pytest.mark.asyncio
async def test_cache_control_absent_on_404(client: AsyncClient) -> None:
    """404s must not be cached — clients shouldn't keep an old absence."""
    async with client as ac:
        r = await ac.get("/nobody/missing")
    assert r.status_code == 404
    assert "cache-control" not in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_cache_control_absent_on_non_charter_routes(client: AsyncClient) -> None:
    async with client as ac:
        r_health = await ac.get("/healthz")
        r_jwks = await ac.get("/.well-known/jwks.json")
        r_log = await ac.get("/transparency/log")
    for r in (r_health, r_jwks, r_log):
        assert r.status_code == 200
        assert "cache-control" not in {k.lower() for k in r.headers}


# ---------------------------------------------------------------------------
# GET /transparency/revoked (AC #2, #3, #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_feed_all_entries(client: AsyncClient) -> None:
    """`?since=0` returns every revoked Charter as NDJSON."""
    c1 = _make_charter(seed="a", principal_id="alice@acme.com", agent_id="agent_one")
    c2 = _make_charter(seed="b", principal_id="alice@acme.com", agent_id="agent_two")
    _revoke_in_place(c1)
    _revoke_in_place(c2)

    async with client as ac:
        r = await ac.get("/transparency/revoked")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    entries = [json.loads(ln) for ln in lines]
    assert {e["charter_id"] for e in entries} == {c1.charter_id, c2.charter_id}
    for e in entries:
        assert set(e.keys()) == {"charter_id", "principal_id", "agent_id", "revoked_at", "seq"}


@pytest.mark.asyncio
async def test_revoked_feed_since_filters(client: AsyncClient) -> None:
    """`since=N` only returns entries with seq > N."""
    c1 = _make_charter(seed="a", principal_id="alice@acme.com", agent_id="agent_one")
    c2 = _make_charter(seed="b", principal_id="alice@acme.com", agent_id="agent_two")
    c3 = _make_charter(seed="c", principal_id="alice@acme.com", agent_id="agent_three")
    _revoke_in_place(c1)
    _revoke_in_place(c2)
    _revoke_in_place(c3)

    async with client as ac:
        r = await ac.get("/transparency/revoked", params={"since": 1})
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    seqs = {json.loads(ln)["seq"] for ln in lines}
    # c1 has seq=1, c2=2, c3=3. since=1 -> only seq=2 and seq=3.
    assert seqs == {2, 3}


@pytest.mark.asyncio
async def test_revoked_feed_excludes_active_charters(client: AsyncClient) -> None:
    """Charters whose status is still active must NOT appear in the feed."""
    active = _make_charter(seed="a", principal_id="alice@acme.com", agent_id="agent_one")
    revoked = _make_charter(seed="b", principal_id="alice@acme.com", agent_id="agent_two")
    _revoke_in_place(revoked)

    async with client as ac:
        r = await ac.get("/transparency/revoked")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    ids = {json.loads(ln)["charter_id"] for ln in lines}
    assert revoked.charter_id in ids
    assert active.charter_id not in ids


@pytest.mark.asyncio
async def test_revoked_feed_empty_when_nothing_revoked(client: AsyncClient) -> None:
    _make_charter()
    async with client as ac:
        r = await ac.get("/transparency/revoked")
    assert r.status_code == 200
    assert r.text.strip() == ""


@pytest.mark.asyncio
async def test_revoked_feed_rejects_negative_since(client: AsyncClient) -> None:
    """Fail-closed: negative since -> 400, not 422 or silent 200."""
    async with client as ac:
        r = await ac.get("/transparency/revoked", params={"since": -1})
    assert r.status_code == 400
    assert "non-negative" in r.json()["detail"]


@pytest.mark.asyncio
async def test_revoked_feed_rejects_non_integer_since(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/transparency/revoked", params={"since": "abc"})
    assert r.status_code == 400
    assert "integer" in r.json()["detail"]


# ---------------------------------------------------------------------------
# subscribe_revocations async generator (AC #5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_revocations_yields_new_entries(temp_data_dir: Path) -> None:
    """The async generator polls the endpoint and yields new entries as
    the cursor advances. Uses the in-process ASGI transport so no real
    network is touched."""
    from charter.server import app

    c1 = _make_charter(seed="a", principal_id="alice@acme.com", agent_id="agent_one")
    _revoke_in_place(c1)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        gen = subscribe_revocations(
            "http://test",
            since=0,
            poll_interval=0.01,
            client=http,
        )
        # First yield: should produce c1's revocation entry.
        first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert isinstance(first, RevocationEntry)
        assert first.charter_id == c1.charter_id
        assert first.principal_id == "alice@acme.com"
        assert first.agent_id == "agent_one"

        # Revoke a second Charter mid-stream — the next poll should
        # surface it because the generator's cursor advanced past c1's seq.
        c2 = _make_charter(seed="b", principal_id="alice@acme.com", agent_id="agent_two")
        _revoke_in_place(c2)

        second = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert second.charter_id == c2.charter_id

        await gen.aclose()


# ---------------------------------------------------------------------------
# RevocationAwareCache (AC #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_aware_cache_evicts_on_match(temp_data_dir: Path) -> None:
    """A cached Charter whose id arrives in the revocation feed must be
    auto-evicted by the background poll task."""
    from charter.server import app

    cached = _make_charter(seed="a", principal_id="alice@acme.com", agent_id="agent_one")
    untouched = _make_charter(seed="b", principal_id="alice@acme.com", agent_id="agent_two")

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        cache = RevocationAwareCache("http://test", poll_interval=0.01, client=http)
        async with cache:
            cache[cached.charter_id] = cached
            cache[untouched.charter_id] = untouched
            assert cached.charter_id in cache
            assert untouched.charter_id in cache

            # Now revoke one of them — the polling task should evict it.
            _revoke_in_place(cached)

            # Wait for the cache to notice (poll_interval is 10ms).
            for _ in range(200):
                if cached.charter_id not in cache:
                    break
                await asyncio.sleep(0.01)

            assert cached.charter_id not in cache, (
                "expected revoked charter to be evicted by the polling task"
            )
            assert untouched.charter_id in cache, "untouched charter must remain cached"


@pytest.mark.asyncio
async def test_revocation_aware_cache_clean_cancel(temp_data_dir: Path) -> None:
    """Exiting the async context cancels the polling task cleanly."""
    from charter.server import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        cache = RevocationAwareCache("http://test", poll_interval=0.01, client=http)
        await cache.start()
        task = cache._task
        assert task is not None
        await cache.aclose()
        assert task.done()


# ---------------------------------------------------------------------------
# RevocationEntry round-trip
# ---------------------------------------------------------------------------


def test_revocation_entry_round_trip() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    entry = RevocationEntry(
        charter_id="charter:alice:agent:2026-05-23",
        principal_id="alice",
        agent_id="agent",
        revoked_at=now,
        seq=42,
    )
    payload: dict[str, Any] = dict(entry.to_dict())
    assert payload["seq"] == 42
    back = RevocationEntry.from_dict(payload)
    assert back == entry


def test_revocation_entry_from_dict_rejects_bad_seq() -> None:
    with pytest.raises(ValueError, match="must be an int"):
        RevocationEntry.from_dict(
            {
                "charter_id": "x",
                "principal_id": "p",
                "agent_id": "a",
                "revoked_at": "2026-05-23T00:00:00+00:00",
                "seq": "not-an-int",
            }
        )


# ---------------------------------------------------------------------------
# iter_revoked_entries — direct module API
# ---------------------------------------------------------------------------


def test_iter_revoked_entries_matches_endpoint(temp_data_dir: Path) -> None:
    """The pure-Python iterator and the HTTP endpoint must agree on what
    counts as revoked."""
    active = _make_charter(seed="a", principal_id="alice@acme.com", agent_id="agent_one")
    revoked = _make_charter(seed="b", principal_id="alice@acme.com", agent_id="agent_two")
    _revoke_in_place(revoked)

    entries = list(revocation.iter_revoked_entries(since=0))
    ids = {e.charter_id for e in entries}
    assert revoked.charter_id in ids
    assert active.charter_id not in ids
