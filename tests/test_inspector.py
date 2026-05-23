"""Tests for the Charter Inspector Web UI (B3.8).

Covers:

  - happy path: 200 + HTML body contains charter_id
  - redacted clauses: `[REDACTED:...]` placeholder rendered
  - revoked charter: red banner present
  - chain: parent link emitted for each parent_charter_url hop
  - invalid charter URL: 400 (bad scheme / private network SSRF)
  - jinja2 not installed: 503 with `pip install charter[inspector]`

The tests monkeypatch `charter.mcp_server._fetch_and_verify` to avoid
real network I/O. Inspector internals (URL validation, diff,
chain-walk) are unit-tested without spinning up the FastAPI app.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from charter.errors import (
    CharterNotFoundError,
    CharterRevokedError,
)
from charter.schema import (
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    PrivateFieldRef,
    Provenance,
    SourceCommitment,
    Summary,
    Visibility,
)
from charter.signing import generate_keypair, public_key_to_string, sign_charter

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from any shared data dir."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    # Inspector tests almost always want the SSRF guard relaxed because
    # the synthetic URLs they invent point at localhost. The bypass is
    # explicit env-var opt-in, so flipping it here mirrors the real
    # local-dev workflow.
    monkeypatch.setenv("CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS", "1")
    return tmp_path


@pytest.fixture
def client(temp_data_dir: Path):  # noqa: ARG001
    from charter.server import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _make_charter(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    status: str = "active",
    with_redaction: bool = False,
    parent_url: str | None = None,
) -> Charter:
    """Build a freshly-signed Charter for use in inspector tests."""
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    clauses = [
        Clause(id="C-001", type="scope", text="May draft research notes."),
        Clause(id="C-002", type="out_of_scope", text="Must not send email."),
    ]
    if with_redaction:
        clauses.append(
            Clause(
                id="C-003",
                type="data_handling",
                text="Process [REDACTED:abc12345] under NDA.",
                private_fields=[
                    PrivateFieldRef(
                        span_start=8,
                        span_end=26,
                        disclosure_hash="sha256:" + "a" * 64,
                    )
                ],
            )
        )
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        visibility=Visibility(
            private_clauses="redaction_v1" if with_redaction else "not_supported_in_v0"
        ),
        summary=Summary(plain_language="Test charter."),
        clauses=clauses,
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
        parent_charter_url=parent_url,
    )
    sign_charter(charter, private)
    return charter


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_happy_path_renders_charter_id(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    charter = _make_charter()
    # Patch `_fetch_and_verify` so we never touch the network. The
    # inspector route imports it inside the handler, so patching the
    # module attribute is what reaches the live code path.
    import charter.mcp_server as mcp

    def fake_fetch(url: str) -> Charter:
        return charter

    monkeypatch.setattr(mcp, "_fetch_and_verify", fake_fetch)

    async with client as ac:
        r = await ac.get(
            "/inspect", params={"url": "http://localhost:8000/alice@acme.com/research_agent_v1"}
        )
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert charter.charter_id in body
    # Header status badge.
    assert "badge-active" in body
    # Both clauses surface in the rendered list.
    assert "C-001" in body
    assert "C-002" in body


# ---------------------------------------------------------------------------
# 2. Redacted clauses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_renders_redacted_marker(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    charter = _make_charter(with_redaction=True)
    import charter.mcp_server as mcp

    monkeypatch.setattr(mcp, "_fetch_and_verify", lambda url: charter)

    async with client as ac:
        r = await ac.get(
            "/inspect", params={"url": "http://localhost:8000/alice@acme.com/research_agent_v1"}
        )
    assert r.status_code == 200
    body = r.text
    # The clause's own text already contains [REDACTED:abc12345]; the
    # template also prints a badge with the count. The presence of the
    # literal `[REDACTED:` substring proves the redaction surface
    # reached the page.
    assert "[REDACTED:" in body
    # First 8 chars of the disclosure_hash should appear (sha256:aaaaaaaa).
    # The template truncates to 14 chars (sha256: + first 7) for
    # display economy.
    assert "sha256:aaaaaaa" in body


# ---------------------------------------------------------------------------
# 3. Revoked charter -> red banner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_revoked_renders_red_banner(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    revoked = _make_charter(status="revoked")
    import charter.mcp_server as mcp

    def fake_fetch(url: str) -> Charter:
        # _fetch_and_verify would raise on a revoked charter; simulate it
        # so the route exercises its "verification failed, render anyway"
        # branch. We additionally need the route's fallback `httpx.get` to
        # return the same charter body — patch httpx for that.
        raise CharterRevokedError("Charter status=revoked at " + url)

    monkeypatch.setattr(mcp, "_fetch_and_verify", fake_fetch)

    # The route falls back to httpx.get(url) on a CharterError so it
    # can still display the body. Stub httpx in the server module.
    import charter.server as srv  # noqa: F401

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeHttpx:
        @staticmethod
        def get(url: str, timeout: float = 10.0) -> _FakeResp:
            return _FakeResp(revoked.model_dump(mode="json"))

    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

    async with client as ac:
        r = await ac.get(
            "/inspect", params={"url": "http://localhost:8000/alice@acme.com/research_agent_v1"}
        )

    # 200: the page IS served (with a red banner). Don't 5xx — that hides
    # the diagnostic content from the browser chrome.
    assert r.status_code == 200
    body = r.text
    assert "banner-error" in body
    assert "CharterRevokedError" in body
    # Clauses must still be visible behind the banner.
    assert "C-001" in body


# ---------------------------------------------------------------------------
# 4. Chain rendering — emits parent link for each hop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_chain_renders_parent_links(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _make_charter(
        principal_id="acme_corp",
        agent_id="root",
    )
    parent_url = "http://localhost:8000/acme_corp/root"
    child = _make_charter(
        principal_id="alice@acme.com",
        agent_id="research_agent_v1",
        parent_url=parent_url,
    )
    child_url = "http://localhost:8000/alice@acme.com/research_agent_v1"

    import charter.mcp_server as mcp

    def fake_fetch(url: str) -> Charter:
        if url == child_url:
            return child
        if url == parent_url:
            return parent
        raise CharterNotFoundError(f"unexpected url: {url}")

    monkeypatch.setattr(mcp, "_fetch_and_verify", fake_fetch)

    async with client as ac:
        r = await ac.get("/inspect", params={"url": child_url})

    assert r.status_code == 200
    body = r.text
    # The chain panel exists (only rendered when chain length > 1).
    assert "Charter chain" in body
    # Parent's charter_id renders as a link in the chain panel.
    assert parent.charter_id in body
    # The parent's URL appears inside an anchor href.
    assert "/inspect?url=" in body


# ---------------------------------------------------------------------------
# 5. Invalid charter_url -> 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_invalid_url_returns_400(client: AsyncClient) -> None:
    async with client as ac:
        # file:// is firmly outside the scheme allowlist
        r = await ac.get("/inspect", params={"url": "file:///etc/passwd"})
    assert r.status_code == 400
    body = r.text
    assert "scheme" in body.lower()
    # The reflected URL should be HTML-escaped (no raw < or > would
    # appear in the URL anyway, but escapes a literal '&' if present).
    assert "file:///etc/passwd" in body  # echoed back for context


@pytest.mark.asyncio
async def test_inspect_ssrf_guard_blocks_private_net(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSRF guard MUST be on by default (no env override).

    We deliberately do NOT use the `temp_data_dir` fixture here because
    it sets `CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS=1` for convenience.
    """
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS", raising=False)

    from charter.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/inspect", params={"url": "http://10.0.0.5/charter"})
    assert r.status_code == 400
    assert "private" in r.text.lower()


# ---------------------------------------------------------------------------
# 6. jinja2 not installed -> 503 with install hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_503_when_jinja2_missing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force `inspector.is_available()` to lie about jinja2's presence by
    # patching the helper on the inspector module — that's the same
    # boundary the route checks.
    import charter.inspector as ins

    monkeypatch.setattr(ins, "is_available", lambda: False)

    async with client as ac:
        r = await ac.get("/inspect", params={"url": "http://localhost:8000/x/y"})
    assert r.status_code == 503
    body = r.text
    assert "pip install charter[inspector]" in body
    assert "jinja2" in body.lower()


# ---------------------------------------------------------------------------
# Bonus unit tests for inspector helpers (these strengthen the QA story
# even though only 6 cases are required by the AC).
# ---------------------------------------------------------------------------


def test_validate_url_rejects_empty() -> None:
    from charter.inspector import InvalidCharterURLError, validate_charter_url

    with pytest.raises(InvalidCharterURLError, match="empty"):
        validate_charter_url("")


def test_validate_url_rejects_gopher() -> None:
    from charter.inspector import InvalidCharterURLError, validate_charter_url

    with pytest.raises(InvalidCharterURLError, match="scheme"):
        validate_charter_url("gopher://example.com/")


def test_validate_url_rejects_overlong(monkeypatch: pytest.MonkeyPatch) -> None:
    from charter.inspector import InvalidCharterURLError, validate_charter_url

    overlong = "https://example.com/" + ("a" * 3000)
    with pytest.raises(InvalidCharterURLError, match="longer than"):
        validate_charter_url(overlong)


def test_validate_url_rejects_loopback_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from charter.inspector import InvalidCharterURLError, validate_charter_url

    monkeypatch.delenv("CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS", raising=False)
    with pytest.raises(InvalidCharterURLError, match="private"):
        validate_charter_url("http://localhost:8000/x/y")


def test_validate_url_allows_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from charter.inspector import validate_charter_url

    monkeypatch.setenv("CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS", "1")
    assert validate_charter_url("http://127.0.0.1/x/y") == "http://127.0.0.1/x/y"


def test_diff_against_prior_returns_none_when_no_replaces() -> None:
    from charter.inspector import diff_against_prior

    charter = _make_charter()
    assert diff_against_prior(charter) is None
