"""Tests for `charter revoke` and `charter renew` CLI commands.

We invoke the CLI via `click.testing.CliRunner` and use a temp data dir
so tests don't touch real Charters or keys.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from charter.cli import cli
from charter.errors import CharterRevokedError
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
from charter.signing import public_key_to_string, sign_charter
from charter.storage import (
    ensure_issuer_key,
    load_archived_charter,
    load_charter,
    save_charter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


def _seed_charter(
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    status: str = "active",
) -> Charter:
    """Seed a signed Charter on disk + a matching issuer key.

    Returns the in-memory Charter so the test can inspect it.
    """
    # Use ensure_issuer_key so subsequent CLI calls reuse the same key.
    private = ensure_issuer_key(principal_id)
    public = private.public_key()

    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting."),
            Clause(id="C-002", type="out_of_scope", text="Marketing."),
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
                    description="seed",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(charter, private)
    save_charter(charter)
    return charter


# ---------------------------------------------------------------------------
# charter revoke
# ---------------------------------------------------------------------------


def test_revoke_flips_status_and_writes_revoked_at(temp_data_dir: Path) -> None:
    _seed_charter()
    runner = CliRunner()

    result = runner.invoke(cli, ["revoke", "alice@acme.com", "research_agent_v1"])

    assert result.exit_code == 0, result.output
    assert "Charter revoked" in result.output

    reloaded = load_charter("alice@acme.com", "research_agent_v1")
    assert reloaded is not None
    assert reloaded.lifecycle.status == "revoked"
    assert reloaded.lifecycle.revoked_at is not None


def test_revoke_re_signs_charter(temp_data_dir: Path) -> None:
    """The revoked Charter's new signature must verify against the embedded key."""
    _seed_charter()
    runner = CliRunner()
    runner.invoke(cli, ["revoke", "alice@acme.com", "research_agent_v1"])

    reloaded = load_charter("alice@acme.com", "research_agent_v1")
    assert reloaded is not None
    # The signature must still verify after the lifecycle mutation + re-sign.
    from charter.signing import verify_charter

    assert verify_charter(reloaded) is True


def test_revoke_causes_fetch_to_raise(temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_charter()
    runner = CliRunner()
    runner.invoke(cli, ["revoke", "alice@acme.com", "research_agent_v1"])

    # Stub fetch to read from disk instead of HTTP.
    revoked = load_charter("alice@acme.com", "research_agent_v1")
    assert revoked is not None

    import httpx

    def fake_get(_url, *, timeout=10.0):  # noqa: ARG001
        req = httpx.Request("GET", "http://test")
        return httpx.Response(200, json=revoked.model_dump(mode="json"), request=req)

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)

    with pytest.raises(CharterRevokedError):
        _fetch_and_verify("http://test/x")


def test_revoke_404s_when_no_charter(temp_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["revoke", "nobody@nowhere.io", "unknown_agent"])
    assert result.exit_code == 1
    assert "No Charter found" in result.output


def test_revoke_refuses_already_revoked(temp_data_dir: Path) -> None:
    _seed_charter(status="revoked")
    runner = CliRunner()
    result = runner.invoke(cli, ["revoke", "alice@acme.com", "research_agent_v1"])
    assert result.exit_code == 1
    assert "already revoked" in result.output


# ---------------------------------------------------------------------------
# charter renew
# ---------------------------------------------------------------------------


def test_renew_creates_new_charter_with_replaces_link(temp_data_dir: Path) -> None:
    original = _seed_charter()
    runner = CliRunner()

    result = runner.invoke(cli, ["renew", "alice@acme.com", "research_agent_v1"])

    assert result.exit_code == 0, result.output
    assert "Charter renewed" in result.output

    live = load_charter("alice@acme.com", "research_agent_v1")
    assert live is not None
    assert live.charter_id != original.charter_id
    assert live.lifecycle.replaces == original.charter_id
    assert live.lifecycle.status == "active"
    # Clauses + summary carried over identically.
    assert [(c.id, c.type, c.text) for c in live.clauses] == [
        (c.id, c.type, c.text) for c in original.clauses
    ]
    assert live.summary.plain_language == original.summary.plain_language


def test_renew_archives_predecessor_with_superseded_status(temp_data_dir: Path) -> None:
    original = _seed_charter()
    runner = CliRunner()
    runner.invoke(cli, ["renew", "alice@acme.com", "research_agent_v1"])

    live = load_charter("alice@acme.com", "research_agent_v1")
    assert live is not None

    archived = load_archived_charter(original.charter_id)
    assert archived is not None
    assert archived.charter_id == original.charter_id
    assert archived.lifecycle.status == "superseded"
    assert archived.lifecycle.replaced_by == live.charter_id


def test_renew_signatures_both_verify(temp_data_dir: Path) -> None:
    _seed_charter()
    runner = CliRunner()
    runner.invoke(cli, ["renew", "alice@acme.com", "research_agent_v1"])

    from charter.signing import verify_charter

    live = load_charter("alice@acme.com", "research_agent_v1")
    assert live is not None and verify_charter(live)

    archived = load_archived_charter(live.lifecycle.replaces or "")
    assert archived is not None and verify_charter(archived)


def test_renew_respects_valid_days_override(temp_data_dir: Path) -> None:
    _seed_charter()
    runner = CliRunner()
    runner.invoke(
        cli,
        ["renew", "alice@acme.com", "research_agent_v1", "--valid-days", "60"],
    )

    live = load_charter("alice@acme.com", "research_agent_v1")
    assert live is not None
    delta = live.lifecycle.valid_until - live.lifecycle.issued_at
    assert delta.days == 60


def test_renew_404s_when_no_charter(temp_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["renew", "nobody@nowhere.io", "unknown_agent"])
    assert result.exit_code == 1
    assert "No Charter found" in result.output


def test_renew_refuses_revoked_charter(temp_data_dir: Path) -> None:
    _seed_charter(status="revoked")
    runner = CliRunner()
    result = runner.invoke(cli, ["renew", "alice@acme.com", "research_agent_v1"])
    assert result.exit_code == 1
    assert "Cannot renew" in result.output


def test_renew_accepts_expired_charter(temp_data_dir: Path) -> None:
    """Renewing an expired Charter is a common case — must work."""
    _seed_charter(status="expired")
    runner = CliRunner()
    result = runner.invoke(cli, ["renew", "alice@acme.com", "research_agent_v1"])
    assert result.exit_code == 0, result.output

    live = load_charter("alice@acme.com", "research_agent_v1")
    assert live is not None
    assert live.lifecycle.status == "active"
