"""Unit tests for `charter/_fetch_and_verify` typed-exception paths.

Each test stubs out `httpx.get` (or `verify_charter`) so the function reaches
exactly one failure branch, then asserts the correct typed exception fires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from charter.errors import (
    CharterError,
    CharterExpiredError,
    CharterNotFoundError,
    CharterRevokedError,
    CharterSchemaError,
    CharterSignatureError,
)
from charter.mcp_server import _fetch_and_verify
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

# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------


def test_all_typed_errors_are_charter_errors():
    for cls in (
        CharterNotFoundError,
        CharterSchemaError,
        CharterSignatureError,
        CharterRevokedError,
        CharterExpiredError,
    ):
        assert issubclass(cls, CharterError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed_charter(*, status: str = "active") -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:test@example.com:agent_x:2026-05-17",
        binding=Binding(principal_id="test@example.com", agent_id="agent_x"),
        principal=Principal(id="test@example.com", role_summary="Test"),
        issuer=Issuer(id="test@example.com"),
        agent_operator=AgentOperator(id="test_operator"),
        summary=Summary(plain_language="Test."),
        clauses=[Clause(id="C-001", type="scope", text="...")],
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


def _stub_httpx(
    monkeypatch, *, status_code: int = 200, json_payload=None, raise_request_error: bool = False
):
    """Replace `httpx.get` inside mcp_server with a controllable stub."""

    def fake_get(_url, *, timeout=10.0):  # noqa: ARG001
        if raise_request_error:
            raise httpx.ConnectError("connection refused")
        req = httpx.Request("GET", "http://test")
        return httpx.Response(status_code, json=json_payload or {}, request=req)

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)


# ---------------------------------------------------------------------------
# CharterNotFoundError — 404 + connection failures
# ---------------------------------------------------------------------------


def test_404_raises_not_found(monkeypatch):
    _stub_httpx(monkeypatch, status_code=404, json_payload={"detail": "nope"})
    with pytest.raises(CharterNotFoundError) as exc:
        _fetch_and_verify("http://test/nope")
    assert "404" in str(exc.value)


def test_connection_error_raises_not_found(monkeypatch):
    _stub_httpx(monkeypatch, raise_request_error=True)
    with pytest.raises(CharterNotFoundError) as exc:
        _fetch_and_verify("http://test/dead")
    assert "failed" in str(exc.value)


def test_5xx_raises_not_found(monkeypatch):
    _stub_httpx(monkeypatch, status_code=503)
    with pytest.raises(CharterNotFoundError):
        _fetch_and_verify("http://test/x")


# ---------------------------------------------------------------------------
# CharterSchemaError — non-Charter JSON body
# ---------------------------------------------------------------------------


def test_garbage_body_raises_schema_error(monkeypatch):
    _stub_httpx(monkeypatch, status_code=200, json_payload={"not": "a charter"})
    with pytest.raises(CharterSchemaError):
        _fetch_and_verify("http://test/x")


# ---------------------------------------------------------------------------
# CharterSignatureError — tampered Charter
# ---------------------------------------------------------------------------


def test_tampered_charter_raises_signature_error(monkeypatch):
    charter = _signed_charter()
    payload = charter.model_dump(mode="json")
    payload["clauses"][0]["text"] = "TAMPERED"
    _stub_httpx(monkeypatch, status_code=200, json_payload=payload)
    with pytest.raises(CharterSignatureError):
        _fetch_and_verify("http://test/x")


# ---------------------------------------------------------------------------
# CharterRevokedError / CharterExpiredError — lifecycle states
# ---------------------------------------------------------------------------


def test_revoked_raises_revoked_error(monkeypatch):
    # Build the charter with status=active, sign it, then flip status post-sign.
    # The signature still verifies because canonical_bytes covers the
    # post-sign payload's status field too — but the test does NOT depend on
    # signature validity, only on the lifecycle check ordering. To keep
    # signature valid, we sign WITH status=revoked from the start.
    charter = _signed_charter(status="revoked")
    payload = charter.model_dump(mode="json")
    _stub_httpx(monkeypatch, status_code=200, json_payload=payload)
    with pytest.raises(CharterRevokedError):
        _fetch_and_verify("http://test/x")


def test_expired_raises_expired_error(monkeypatch):
    charter = _signed_charter(status="expired")
    payload = charter.model_dump(mode="json")
    _stub_httpx(monkeypatch, status_code=200, json_payload=payload)
    with pytest.raises(CharterExpiredError):
        _fetch_and_verify("http://test/x")


def test_superseded_raises_expired_error(monkeypatch):
    charter = _signed_charter(status="superseded")
    payload = charter.model_dump(mode="json")
    _stub_httpx(monkeypatch, status_code=200, json_payload=payload)
    with pytest.raises(CharterExpiredError):
        _fetch_and_verify("http://test/x")


# ---------------------------------------------------------------------------
# Happy path — confirms no exception
# ---------------------------------------------------------------------------


def test_active_charter_returns_normally(monkeypatch):
    charter = _signed_charter(status="active")
    payload = charter.model_dump(mode="json")
    _stub_httpx(monkeypatch, status_code=200, json_payload=payload)
    result = _fetch_and_verify("http://test/x")
    assert result.lifecycle.status == "active"
    assert result.charter_id == charter.charter_id
