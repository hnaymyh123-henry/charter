"""Tests for `charter._logging` and the fetch-path log emissions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from charter._logging import configure_logging, get_logger
from charter.errors import (
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
# Configuration
# ---------------------------------------------------------------------------


def test_get_logger_rejects_non_charter_names() -> None:
    with pytest.raises(ValueError, match="must start with"):
        get_logger("foo.bar")


def test_configure_human_format_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHARTER_LOG_FORMAT", raising=False)
    configure_logging()
    root = logging.getLogger("charter")
    assert len(root.handlers) == 1
    # Default level should be INFO.
    assert root.level == logging.INFO


def test_configure_json_format_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_LOG_FORMAT", "json")
    configure_logging()
    formatter = logging.getLogger("charter").handlers[0].formatter
    assert formatter is not None
    assert formatter.__class__.__name__ == "_JsonFormatter"


def test_configure_human_format_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_LOG_FORMAT", "human")
    configure_logging()
    formatter = logging.getLogger("charter").handlers[0].formatter
    assert formatter is not None
    assert formatter.__class__.__name__ == "_HumanFormatter"


def test_configure_replaces_existing_handlers() -> None:
    configure_logging()
    configure_logging()
    assert len(logging.getLogger("charter").handlers) == 1


def test_log_level_override() -> None:
    configure_logging(level="DEBUG")
    assert logging.getLogger("charter").level == logging.DEBUG
    configure_logging(level="INFO")  # restore


# ---------------------------------------------------------------------------
# JSON formatter output shape
# ---------------------------------------------------------------------------


def test_json_format_includes_extras(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(fmt="json")
    log = get_logger("charter.fetch")
    with caplog.at_level(logging.INFO, logger="charter"):
        log.info("test message", extra={"charter_id": "x", "outcome": "ok"})

    # caplog captures the LogRecord; assert the extras made it onto the record.
    rec = next(r for r in caplog.records if r.name == "charter.fetch")
    assert rec.message == "test message"
    assert rec.charter_id == "x"
    assert rec.outcome == "ok"


def test_json_formatter_produces_parseable_json() -> None:
    """End-to-end: pipe a record through the JSON formatter directly."""
    from charter._logging import _JsonFormatter

    fmt = _JsonFormatter()
    record = logging.LogRecord(
        name="charter.fetch",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.charter_id = "abc"  # type: ignore[attr-defined]
    record.outcome = "ok"  # type: ignore[attr-defined]
    line = fmt.format(record)
    parsed = json.loads(line)
    assert parsed["msg"] == "hello"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "charter.fetch"
    assert parsed["charter_id"] == "abc"
    assert parsed["outcome"] == "ok"
    # Timestamp is ISO 8601 — sanity check.
    datetime.fromisoformat(parsed["ts"])


# ---------------------------------------------------------------------------
# fetch_and_verify emits exactly one log line per outcome
# ---------------------------------------------------------------------------


def _signed_charter(status: str = "active") -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:test@example.com:agent_x:2026-05-18",
        binding=Binding(principal_id="test@example.com", agent_id="agent_x"),
        principal=Principal(id="test@example.com", role_summary="Test"),
        issuer=Issuer(id="test@example.com"),
        agent_operator=AgentOperator(id="generic"),
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


@pytest.fixture(autouse=True)
def _isolate_pin_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each fetch test gets its own pin file. Fresh keypairs per test
    would otherwise mismatch a pin recorded by an earlier test."""
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))


def _stub_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    json_payload: Any = None,
    raise_request_error: bool = False,
) -> None:
    """Replace `httpx.get` for both Charter fetch and the v0.8 JWKS lookup.

    JWKS publishes the Charter's own key under its `issuer_kid`, so the
    v0.8 cross-check passes and these tests stay about Charter-layer
    behavior.
    """
    from charter import keys as keys_mod
    from charter.signing import public_key_to_jwk

    keys_mod.clear_cache()

    def _build_jwks_body() -> dict:
        if not isinstance(json_payload, dict):
            return {"keys": []}
        prov = json_payload.get("provenance") or {}
        kid = prov.get("issuer_kid")
        pk = prov.get("issuer_public_key")
        if not isinstance(kid, str) or not isinstance(pk, str):
            return {"keys": []}
        jwk = public_key_to_jwk(pk, kid=kid)
        return {"keys": [jwk]}

    def fake_get(url, *, timeout=10.0):  # noqa: ARG001
        if "/.well-known/jwks.json" in url:
            req = httpx.Request("GET", url)
            return httpx.Response(200, json=_build_jwks_body(), request=req)
        if raise_request_error:
            raise httpx.ConnectError("connection refused")
        req = httpx.Request("GET", "http://test")
        return httpx.Response(status_code, json=json_payload or {}, request=req)

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)
    monkeypatch.setattr("charter.keys.httpx.get", fake_get)


def _outcomes(caplog: pytest.LogCaptureFixture, logger: str = "charter.fetch") -> list[str]:
    return [
        getattr(r, "outcome", "")
        for r in caplog.records
        if r.name == logger and hasattr(r, "outcome")
    ]


def test_fetch_success_emits_ok(monkeypatch, caplog) -> None:
    charter = _signed_charter()
    _stub_httpx(monkeypatch, status_code=200, json_payload=charter.model_dump(mode="json"))

    with caplog.at_level(logging.INFO, logger="charter.fetch"):
        _fetch_and_verify("http://test/x")

    assert _outcomes(caplog) == ["ok"]


def test_fetch_404_emits_not_found(monkeypatch, caplog) -> None:
    _stub_httpx(monkeypatch, status_code=404)

    with (
        caplog.at_level(logging.WARNING, logger="charter.fetch"),
        pytest.raises(CharterNotFoundError),
    ):
        _fetch_and_verify("http://test/x")

    assert _outcomes(caplog) == ["not_found"]


def test_fetch_schema_error_emits(monkeypatch, caplog) -> None:
    _stub_httpx(monkeypatch, status_code=200, json_payload={"not": "a charter"})

    with (
        caplog.at_level(logging.WARNING, logger="charter.fetch"),
        pytest.raises(CharterSchemaError),
    ):
        _fetch_and_verify("http://test/x")

    assert _outcomes(caplog) == ["schema_error"]


def test_fetch_signature_error_emits(monkeypatch, caplog) -> None:
    charter = _signed_charter()
    payload = charter.model_dump(mode="json")
    payload["clauses"][0]["text"] = "TAMPERED"
    _stub_httpx(monkeypatch, status_code=200, json_payload=payload)

    with (
        caplog.at_level(logging.ERROR, logger="charter.fetch"),
        pytest.raises(CharterSignatureError),
    ):
        _fetch_and_verify("http://test/x")

    assert _outcomes(caplog) == ["signature_error"]


def test_fetch_revoked_emits(monkeypatch, caplog) -> None:
    charter = _signed_charter(status="revoked")
    _stub_httpx(monkeypatch, status_code=200, json_payload=charter.model_dump(mode="json"))

    with (
        caplog.at_level(logging.WARNING, logger="charter.fetch"),
        pytest.raises(CharterRevokedError),
    ):
        _fetch_and_verify("http://test/x")

    assert _outcomes(caplog) == ["revoked"]


def test_fetch_expired_emits(monkeypatch, caplog) -> None:
    charter = _signed_charter(status="expired")
    _stub_httpx(monkeypatch, status_code=200, json_payload=charter.model_dump(mode="json"))

    with (
        caplog.at_level(logging.WARNING, logger="charter.fetch"),
        pytest.raises(CharterExpiredError),
    ):
        _fetch_and_verify("http://test/x")

    assert _outcomes(caplog) == ["expired"]


def test_fetch_each_log_carries_url(monkeypatch, caplog) -> None:
    """Every outcome log line carries the URL field for traceability."""
    _stub_httpx(monkeypatch, status_code=404)
    with (
        caplog.at_level(logging.WARNING, logger="charter.fetch"),
        pytest.raises(CharterNotFoundError),
    ):
        _fetch_and_verify("http://test/specific-url")

    record = next(r for r in caplog.records if r.name == "charter.fetch")
    assert record.url == "http://test/specific-url"
