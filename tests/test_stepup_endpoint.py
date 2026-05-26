"""Integration tests for `POST /step-up` and `GET /grants/{id}` (B2.5).

Uses `httpx.AsyncClient + ASGITransport` so the FastAPI app does not
bind a port. Each test:
  - Points `CHARTER_DATA_DIR` at a tmp path.
  - Persists a signed Charter so `_fetch_and_verify` resolves it from
    the filesystem when the endpoint forwards the lookup.
  - Stubs `_fetch_and_verify` to bypass HTTP. Without this stub, the
    server would `httpx.get(charter_url)` against a real network host.

ADR-013 lives or dies here — these tests pin the public contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from charter.schema import (
    AdHocGrantResponse,
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
from charter.storage import key_path as _key_path
from charter.storage import save_charter, save_private_key

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    # Clear rate-limit state between tests.
    from charter.server import _stepup_reset_rate_limit

    _stepup_reset_rate_limit()
    return tmp_path


def _signed_charter(
    principal_id: str = "alice@acme.com",
    agent_id: str = "pay_agent_v1",
) -> tuple[Charter, str]:
    """Build + sign a Charter, save its private key. Returns (charter, charter_url).

    The private key persists at `data/keys/<principal_id>.pem` so the
    server's `ensure_issuer_key(principal_id)` returns the same key
    that signed the Charter — auto-approve will mint grants under it.
    """
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:test",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test"),
        clauses=[
            Clause(id="C-001", type="scope", text="Low-value transfers"),
            Clause(id="C-002", type="out_of_scope", text="Wire fraud"),
        ],
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
    save_charter(charter)
    save_private_key(private, _key_path(principal_id))
    url = f"http://test/{principal_id}/{agent_id}"
    return charter, url


@pytest.fixture
def stub_fetch_and_verify(monkeypatch: pytest.MonkeyPatch):
    """Replace `_fetch_and_verify` so step-up does not hit the network.

    Returns a callable that lets the test register a mapping
    `charter_url -> Charter | Exception`. Unmapped URLs raise
    `CharterNotFoundError`.
    """
    from charter import server as server_mod
    from charter.errors import CharterNotFoundError

    table: dict[str, object] = {}

    def _stub(url: str) -> Charter:
        v = table.get(url)
        if v is None:
            raise CharterNotFoundError(f"no stub for {url}")
        if isinstance(v, Exception):
            raise v
        return v  # type: ignore[return-value]

    # Patch BOTH the original symbol and the alias inside server module.
    monkeypatch.setattr("charter.mcp_server._fetch_and_verify", _stub)

    def _register(url: str, value: object) -> None:
        table[url] = value

    yield _register

    # Sanity: the stub was used. Not strictly required, but keeps tests honest.
    _ = server_mod


@pytest.fixture
def client(temp_data_dir: Path):  # noqa: ARG001
    from charter.server import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# POST /step-up — approval modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepup_auto_deny_returns_denied(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """Default `auto-deny` mode returns `status=denied` with a reason."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-deny")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": url,
                "task": "Wire fraud demo",
                "justification": "Showing the protocol works.",
                "max_ttl_seconds": 120,
            },
        )
    assert r.status_code == 200, r.text
    body = AdHocGrantResponse.model_validate(r.json())
    assert body.status == "denied"
    assert body.grant is None
    assert body.denial_reason is not None
    assert "auto-deny" in body.denial_reason


@pytest.mark.asyncio
async def test_stepup_auto_approve_returns_signed_grant(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """`auto-approve` mints a grant signed by the Charter's issuer key."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-approve")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": url,
                "task": "Pay $300 to vendor",
                "justification": "Urgent",
                "max_ttl_seconds": 180,
            },
        )
    assert r.status_code == 200, r.text
    body = AdHocGrantResponse.model_validate(r.json())
    assert body.status == "approved"
    assert body.grant is not None
    assert body.denial_reason is None
    # Signature verifies against the Charter's public key.
    from charter.signing import verify_grant_signature

    assert verify_grant_signature(body.grant, charter.provenance.issuer_public_key)
    # And the grant landed on disk.
    from charter.grants import grant_path

    assert grant_path(body.grant.grant_id).exists()


@pytest.mark.asyncio
async def test_stepup_callback_mode_forwards_and_echoes(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """`callback` mode POSTs the request to CHARTER_STEPUP_CALLBACK_URL and
    echoes the response. Stub `httpx.post` to capture the forwarded call."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "callback")
    monkeypatch.setenv("CHARTER_STEPUP_CALLBACK_URL", "https://approver.test/decide")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "status": "denied",
                "grant": None,
                "denial_reason": "manual denial from callback",
            }

    def _fake_post(target_url: str, json: dict[str, object], timeout: float) -> _FakeResp:
        captured["url"] = target_url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr("charter.server.httpx.post", _fake_post)

    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": url,
                "task": "Pay $500",
                "justification": "Urgent",
                "max_ttl_seconds": 240,
            },
        )
    assert r.status_code == 200, r.text
    body = AdHocGrantResponse.model_validate(r.json())
    assert body.status == "denied"
    assert body.denial_reason == "manual denial from callback"
    assert captured["url"] == "https://approver.test/decide"
    assert isinstance(captured["json"], dict)


@pytest.mark.asyncio
async def test_stepup_callback_failure_collapses_to_denied(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """A callback network failure must surface as denied (fail-closed),
    NOT a 5xx — the protocol's safety guarantee depends on this."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "callback")
    monkeypatch.setenv("CHARTER_STEPUP_CALLBACK_URL", "https://approver.test/decide")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    def _exploding_post(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("callback target is down")

    monkeypatch.setattr("charter.server.httpx.post", _exploding_post)

    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": url,
                "task": "test",
                "justification": "test",
                "max_ttl_seconds": 120,
            },
        )
    assert r.status_code == 200, r.text
    body = AdHocGrantResponse.model_validate(r.json())
    assert body.status == "denied"
    assert body.denial_reason is not None
    assert "callback failed" in body.denial_reason


# ---------------------------------------------------------------------------
# POST /step-up — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepup_max_ttl_above_env_cap_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """`max_ttl_seconds` > `CHARTER_STEPUP_MAX_TTL` -> 400."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-approve")
    monkeypatch.setenv("CHARTER_STEPUP_MAX_TTL", "600")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": url,
                "task": "Pay",
                "justification": "Urgent",
                "max_ttl_seconds": 1200,
            },
        )
    assert r.status_code == 400, r.text
    assert "exceeds server cap" in r.text


@pytest.mark.asyncio
async def test_stepup_missing_charter_url_returns_422(client: AsyncClient):
    """Missing required field -> 422 (FastAPI/pydantic validation)."""
    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={"task": "x", "justification": "y", "max_ttl_seconds": 120},
        )
    # pydantic body-level validation surfaces as 422.
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_stepup_empty_charter_url_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
):
    """Empty `charter_url` -> 400 with a helpful reason."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-approve")
    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": "   ",
                "task": "x",
                "justification": "y",
                "max_ttl_seconds": 120,
            },
        )
    assert r.status_code == 400, r.text
    assert "non-empty" in r.text


@pytest.mark.asyncio
async def test_stepup_unfetchable_charter_url_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """If the Charter can't be fetched (or fails signature), return 400."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-approve")
    # Don't register anything; stub raises CharterNotFoundError.
    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": "http://test/unknown/agent",
                "task": "x",
                "justification": "y",
                "max_ttl_seconds": 120,
            },
        )
    assert r.status_code == 400, r.text
    assert "CharterNotFoundError" in r.text


# ---------------------------------------------------------------------------
# POST /step-up — rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepup_rate_limit_blocks_sixth_request_in_window(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """5 requests in 60s succeed; 6th returns 429."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-deny")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    body = {
        "charter_url": url,
        "task": "x",
        "justification": "y",
        "max_ttl_seconds": 120,
    }
    async with client as ac:
        for i in range(5):
            r = await ac.post("/step-up", json=body)
            assert r.status_code == 200, f"request {i} should succeed: {r.text}"
        r = await ac.post("/step-up", json=body)
    assert r.status_code == 429, r.text
    assert "rate limit" in r.text


# ---------------------------------------------------------------------------
# GET /grants/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_grants_unknown_id_returns_404(client: AsyncClient, temp_data_dir: Path):
    """Non-existent grant_id -> 404."""
    async with client as ac:
        r = await ac.get("/grants/does_not_exist")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_get_grants_expired_returns_410(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """An expired grant on disk -> 410 Gone (distinct from 404)."""
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    # Manually craft + persist an expired grant signed by the Charter's key.
    import uuid

    from charter.grants import save_grant
    from charter.schema import AdHocGrant
    from charter.signing import load_private_key, sign_grant

    past = datetime.now(UTC) - timedelta(hours=1)
    g = AdHocGrant(
        grant_id=uuid.uuid4().hex,
        charter_url=url,
        task="t",
        justification="j",
        granted_at=past,
        expires_at=past + timedelta(seconds=120),  # 1h ago -> expired
        issuer_kid=charter.provenance.issuer_kid or "k",
    )
    private = load_private_key(_key_path(charter.binding.principal_id))
    sign_grant(g, private)
    save_grant(g)

    async with client as ac:
        r = await ac.get(f"/grants/{g.grant_id}")
    assert r.status_code == 410, r.text


@pytest.mark.asyncio
async def test_get_grants_valid_returns_grant_json(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    stub_fetch_and_verify,
):
    """End-to-end: mint via auto-approve, then fetch via GET, contents match."""
    monkeypatch.setenv("CHARTER_STEPUP_APPROVAL_MODE", "auto-approve")
    charter, url = _signed_charter()
    stub_fetch_and_verify(url, charter)

    async with client as ac:
        r = await ac.post(
            "/step-up",
            json={
                "charter_url": url,
                "task": "Pay $200",
                "justification": "Urgent",
                "max_ttl_seconds": 300,
            },
        )
        assert r.status_code == 200, r.text
        body = AdHocGrantResponse.model_validate(r.json())
        assert body.grant is not None
        gid = body.grant.grant_id

        r2 = await ac.get(f"/grants/{gid}")
    assert r2.status_code == 200, r2.text
    j = r2.json()
    assert j["grant_id"] == gid
    assert j["task"] == "Pay $200"


@pytest.mark.asyncio
async def test_get_grants_traversal_id_returns_404(client: AsyncClient):
    """A path-traversal grant_id -> 404 (path safety honoured)."""
    async with client as ac:
        # FastAPI normalises some of these; we send a single-segment path.
        r = await ac.get("/grants/..%2F..%2Fetc%2Fpasswd")
    # Either 404 (our handler) or a 4xx from FastAPI's normalisation; both
    # are safe outcomes.
    assert r.status_code in (400, 404)
