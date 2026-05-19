"""Tests for v0.8 #6: `charter audit` CLI + housekeeping.

Covers:
  - `charter audit verify` exits 0 on clean local log.
  - `charter audit verify` exits 1 on tampered local log.
  - `charter audit verify --since N` skips entries with seq <= N.
  - `charter audit verify --remote URL` fetches /transparency/log.
  - `charter audit verify --remote URL` exits 2 on network failure.
  - `charter audit show <id>` displays the entry + related entries.
  - `charter audit show <id>` exits 1 on unknown charter_id.
  - `pyproject.toml` version is `0.8.0` (bumped from the hackathon 0.1.0).
  - `[tool.bumpversion]` config exists and targets pyproject.toml.
  - `CHANGELOG.md` has the v0.5 / v0.6 / v0.7 / v0.8 backfill.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from charter import transparency
from charter.cli import cli
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

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))
    return tmp_path


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
# charter audit verify (local)
# ---------------------------------------------------------------------------


def test_verify_clean_local_log(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify"])
    assert result.exit_code == 0, result.output
    assert "[OK] Transparency log verified" in result.output
    assert "entries:       2" in result.output


def test_verify_empty_local_log(temp_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify"])
    assert result.exit_code == 0, result.output
    assert "(log is empty)" in result.output


def test_verify_detects_tampered_local_log(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")

    # Corrupt seq=2's prev_hash.
    path = transparency.log_file_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = "sha256:" + "f" * 64
    lines[1] = json.dumps(second)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify"])
    assert result.exit_code == 1
    # Click's CliRunner captures stderr into result.output by default
    # (mix_stderr=True); the error message ends up there.
    assert "[ERROR] Chain broken at seq 2" in result.output


def test_verify_since_skips_earlier_entries(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")
    _make_charter(seed="c")

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify", "--since", "1"])
    assert result.exit_code == 0, result.output
    assert "entries:       2" in result.output  # only seq 2 + 3 walked
    assert "range:         seq 2 -> seq 3" in result.output


# ---------------------------------------------------------------------------
# charter audit verify --remote
# ---------------------------------------------------------------------------


def test_verify_remote_success(temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a local log, then stub httpx so --remote fetches its NDJSON."""
    _make_charter(seed="a")
    _make_charter(seed="b")

    # Re-emit the local log as the body /transparency/log would return.
    ndjson_body = "\n".join(
        json.dumps(e.to_dict(), ensure_ascii=False) for e in transparency.read_log()
    )

    def fake_get(url, *, params=None, timeout=30.0):  # noqa: ARG001
        assert "/transparency/log" in url
        req = httpx.Request("GET", url)
        return httpx.Response(200, text=ndjson_body, request=req)

    # cli imports httpx lazily inside _fetch_remote_log; patch httpx.get
    # globally so the lazy import sees the stub.
    monkeypatch.setattr("httpx.get", fake_get)

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify", "--remote", "http://issuer.example.com"])
    assert result.exit_code == 0, result.output
    assert "(remote)" in result.output


def test_verify_remote_404_exits_2(monkeypatch: pytest.MonkeyPatch, temp_data_dir: Path) -> None:
    def fake_get(url, *, params=None, timeout=30.0):  # noqa: ARG001
        req = httpx.Request("GET", url)
        return httpx.Response(
            404,
            text='{"detail":"nope"}',
            request=req,
        )

    monkeypatch.setattr("httpx.get", fake_get)

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify", "--remote", "http://issuer.example.com"])
    assert result.exit_code == 2
    assert "[ERROR]" in result.output


def test_verify_remote_connect_error_exits_2(
    monkeypatch: pytest.MonkeyPatch, temp_data_dir: Path
) -> None:
    def fake_get(url, *, params=None, timeout=30.0):  # noqa: ARG001
        raise httpx.ConnectError("conn refused")

    monkeypatch.setattr("httpx.get", fake_get)

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "verify", "--remote", "http://dead.example.com"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# charter audit show
# ---------------------------------------------------------------------------


def test_show_displays_entry_fields(temp_data_dir: Path) -> None:
    c = _make_charter()
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "show", c.charter_id])
    assert result.exit_code == 0, result.output
    assert c.charter_id in result.output
    assert "seq:" in result.output
    assert "entry_hash:" in result.output
    assert "prev_hash:" in result.output


def test_show_lists_related_same_principal(temp_data_dir: Path) -> None:
    """Three Charters from alice, one from bob — show on the first alice
    Charter lists the other two alice entries but NOT bob."""
    alice_a = _make_charter(principal_id="alice@acme.com", agent_id="agent_a")
    _make_charter(principal_id="alice@acme.com", agent_id="agent_b")
    _make_charter(principal_id="alice@acme.com", agent_id="agent_c")
    _make_charter(principal_id="bob@startup.io", agent_id="agent_x")

    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "show", alice_a.charter_id])
    assert result.exit_code == 0
    assert "Other entries from alice@acme.com (2):" in result.output
    assert "agent_b" in result.output
    assert "agent_c" in result.output
    assert "bob@startup.io" not in result.output


def test_show_unknown_charter_exits_1(temp_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "show", "charter:nobody:agent:2026-01-01"])
    assert result.exit_code == 1
    assert "No transparency entry found" in result.output


# ---------------------------------------------------------------------------
# Housekeeping: pyproject + CHANGELOG
# ---------------------------------------------------------------------------


def test_pyproject_version_bumped_to_0_8() -> None:
    """The package version should match the active release, not the
    hackathon-era 0.1.0."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.8.0"' in pyproject
    assert 'version = "0.1.0"' not in pyproject


def test_pyproject_has_bumpversion_config() -> None:
    """One-command bumps via `bump-my-version bump <part>`."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.bumpversion]" in pyproject
    assert 'current_version = "0.8.0"' in pyproject
    # Targets pyproject.toml itself so the source of truth stays one place.
    assert "[[tool.bumpversion.files]]" in pyproject


def test_changelog_exists_with_backfill() -> None:
    """CHANGELOG.md should exist and cover v0.5 through v0.8."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for version in ("0.8.0", "0.7.0", "0.6.0", "0.5.0"):
        assert f"## [{version}]" in changelog, f"missing section for {version}"
