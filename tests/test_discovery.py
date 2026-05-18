"""Tests for `charter.discovery.resolve_charter_url` and the index-file
side effect from `storage.save_charter`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from charter.discovery import resolve_charter_url, update_index
from charter.errors import CharterNotFoundError
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
from charter.storage import charters_dir, save_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


def _signed_charter(
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
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
        clauses=[Clause(id="C-001", type="scope", text="...")],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
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
# resolve_charter_url — fallback path (no index)
# ---------------------------------------------------------------------------


def test_falls_back_to_default_when_no_index(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://charter.example.com")
    url = resolve_charter_url("alice@acme.com", "research_agent_v1")
    assert url == "https://charter.example.com/alice@acme.com/research_agent_v1"


def test_falls_back_to_protocol_default_when_no_env(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHARTER_URL_BASE", raising=False)
    url = resolve_charter_url("alice@acme.com", "research_agent_v1")
    assert url == "http://localhost:8000/alice@acme.com/research_agent_v1"


def test_explicit_base_overrides_env(temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://wrong.example.com")
    url = resolve_charter_url(
        "alice@acme.com",
        "research_agent_v1",
        base="https://override.example.com",
    )
    assert url == "https://override.example.com/alice@acme.com/research_agent_v1"


def test_strict_mode_raises_on_unknown_binding(temp_data_dir: Path) -> None:
    with pytest.raises(CharterNotFoundError, match="No local index entry"):
        resolve_charter_url("nobody@nowhere.io", "agent_x", strict=True)


# ---------------------------------------------------------------------------
# resolve_charter_url — local index path
# ---------------------------------------------------------------------------


def test_index_lookup_wins_over_fallback(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://fallback.example.com")
    # Manually seed the index with a different URL than the fallback would produce.
    charters_dir()  # ensure dir exists
    update_index(_signed_charter(), base="https://indexed.example.com")

    url = resolve_charter_url("alice@acme.com", "research_agent_v1")
    assert url == "https://indexed.example.com/alice@acme.com/research_agent_v1"


def test_strict_mode_returns_indexed_url_when_known(temp_data_dir: Path) -> None:
    update_index(_signed_charter(), base="https://known.example.com")
    url = resolve_charter_url("alice@acme.com", "research_agent_v1", strict=True)
    assert url == "https://known.example.com/alice@acme.com/research_agent_v1"


def test_corrupt_index_falls_back_silently(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://default.example.com")
    # Write garbage at the index path; helper should silently recover.
    (charters_dir() / "index.json").write_text("not json", encoding="utf-8")

    url = resolve_charter_url("alice@acme.com", "research_agent_v1")
    assert url == "https://default.example.com/alice@acme.com/research_agent_v1"


# ---------------------------------------------------------------------------
# save_charter side effect — index is kept in sync
# ---------------------------------------------------------------------------


def test_save_charter_writes_index_entry(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://saved.example.com")
    save_charter(_signed_charter("alice@acme.com", "research_agent_v1"))

    index = json.loads((charters_dir() / "index.json").read_text())
    assert index == {
        "alice@acme.com": {
            "research_agent_v1": "https://saved.example.com/alice@acme.com/research_agent_v1"
        }
    }


def test_save_charter_extends_existing_index(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_URL_BASE", "https://saved.example.com")
    save_charter(_signed_charter("alice@acme.com", "agent_a"))
    save_charter(_signed_charter("alice@acme.com", "agent_b"))
    save_charter(_signed_charter("bob@startup.io", "agent_a"))

    index = json.loads((charters_dir() / "index.json").read_text())
    assert set(index.keys()) == {"alice@acme.com", "bob@startup.io"}
    assert set(index["alice@acme.com"].keys()) == {"agent_a", "agent_b"}
    assert set(index["bob@startup.io"].keys()) == {"agent_a"}


def test_save_charter_overwrites_same_binding_entry(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewing or re-issuing a binding should leave exactly one entry."""
    monkeypatch.setenv("CHARTER_URL_BASE", "https://saved.example.com")
    save_charter(_signed_charter())
    save_charter(_signed_charter())  # same binding, second time

    index = json.loads((charters_dir() / "index.json").read_text())
    assert len(index["alice@acme.com"]) == 1


def test_list_charters_skips_index_file(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's root index endpoint must not list the index.json file."""
    from charter.storage import list_charters

    save_charter(_signed_charter())
    charters = list_charters()
    assert len(charters) == 1
    assert charters[0].binding.principal_id == "alice@acme.com"
