"""Tests for v0.8 #4: transparency log writer.

Covers:
  - `append` creates the log on first call.
  - Each entry's `prev_hash` matches the previous entry's `entry_hash`.
  - First entry's `prev_hash` is the genesis value (all zeros).
  - `entry_hash` is content-addressed and changes if any field is tampered.
  - `append` is idempotent on `charter_id` (retry-safe).
  - `read_log` yields entries in seq order.
  - `head` returns the last entry.
  - `get_entry` returns by charter_id or None.
  - `verify_chain` reports ok on a clean log.
  - `verify_chain` detects in-place tampering (prev_hash and entry_hash flavors).
  - The log stores ONLY metadata + signature — no clause text leaks.
  - `sign_charter` triggers `append` so issuance is automatically logged.
  - `CHARTER_TRANSPARENCY_LOG` env var relocates the log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from charter.transparency import (
    GENESIS_PREV_HASH,
    TransparencyEntry,
    append,
    get_entry,
    head,
    log_file_path,
    read_log,
    verify_chain,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate `CHARTER_DATA_DIR` (and therefore the transparency log + pin
    file) per test."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))
    return tmp_path


def _make_charter(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    seed: str = "",
) -> Charter:
    """Build a SIGNED Charter (so it has a real kid + signature)."""
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
        clauses=[Clause(id="C-001", type="scope", text="SECRET-CLAUSE-TEXT-must-not-leak")],
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
# log_file_path / env override
# ---------------------------------------------------------------------------


def test_log_file_path_default(temp_data_dir: Path) -> None:
    assert log_file_path() == temp_data_dir / "transparency.log"


def test_log_file_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "elsewhere" / "log.ndjson"
    monkeypatch.setenv("CHARTER_TRANSPARENCY_LOG", str(custom))
    assert log_file_path() == custom


# ---------------------------------------------------------------------------
# append + read_log
# ---------------------------------------------------------------------------


def test_append_creates_log_on_first_call(temp_data_dir: Path) -> None:
    assert not log_file_path().exists()
    _make_charter()  # sign triggers append
    assert log_file_path().exists()


def test_first_entry_has_genesis_prev_hash(temp_data_dir: Path) -> None:
    _make_charter()
    entries = list(read_log())
    assert len(entries) == 1
    assert entries[0].prev_hash == GENESIS_PREV_HASH
    assert entries[0].seq == 1


def test_subsequent_entries_chain_to_predecessor(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")
    _make_charter(seed="c")

    entries = list(read_log())
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS_PREV_HASH
    for i in range(1, len(entries)):
        assert entries[i].prev_hash == entries[i - 1].entry_hash


def test_entry_hash_is_content_addressed(temp_data_dir: Path) -> None:
    """Two distinct Charters produce distinct entry hashes; appending
    the SAME Charter twice is idempotent and produces the same hash."""
    c1 = _make_charter(seed="x")
    _make_charter(seed="y")

    entries = list(read_log())
    assert entries[0].entry_hash != entries[1].entry_hash

    # Re-append c1 — should be a no-op.
    e_again = append(c1)
    assert e_again.entry_hash == entries[0].entry_hash
    assert len(list(read_log())) == 2  # still only two entries


def test_append_returns_correct_seq(temp_data_dir: Path) -> None:
    c1 = _make_charter(seed="1")
    c2 = _make_charter(seed="2")
    e1 = get_entry(c1.charter_id)
    e2 = get_entry(c2.charter_id)
    assert e1 is not None and e2 is not None
    assert e1.seq == 1
    assert e2.seq == 2


# ---------------------------------------------------------------------------
# head + get_entry
# ---------------------------------------------------------------------------


def test_head_empty_log_returns_none(temp_data_dir: Path) -> None:
    assert head() is None


def test_head_returns_last_entry(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    last_charter = _make_charter(seed="b")
    h = head()
    assert h is not None
    assert h.charter_id == last_charter.charter_id


def test_get_entry_returns_match(temp_data_dir: Path) -> None:
    c = _make_charter()
    found = get_entry(c.charter_id)
    assert found is not None
    assert found.charter_id == c.charter_id


def test_get_entry_unknown_returns_none(temp_data_dir: Path) -> None:
    _make_charter()
    assert get_entry("charter:unknown:agent:2026-01-01") is None


# ---------------------------------------------------------------------------
# Log content boundary: no Charter body
# ---------------------------------------------------------------------------


def test_log_does_not_store_clause_text(temp_data_dir: Path) -> None:
    """The Charter body (clauses, summary) MUST NOT appear in the log.
    Only metadata + signature + chain hashes."""
    _make_charter()
    log_text = log_file_path().read_text(encoding="utf-8")
    assert "SECRET-CLAUSE-TEXT-must-not-leak" not in log_text
    assert "Test." not in log_text  # summary text
    assert "role_summary" not in log_text


def test_log_stores_expected_fields(temp_data_dir: Path) -> None:
    c = _make_charter()
    line = log_file_path().read_text(encoding="utf-8").strip()
    raw = json.loads(line)
    assert set(raw.keys()) == {
        "seq",
        "charter_id",
        "binding",
        "issuer_kid",
        "issuer_signature",
        "appended_at",
        "prev_hash",
        "entry_hash",
    }
    assert raw["charter_id"] == c.charter_id
    assert raw["binding"]["principal_id"] == c.binding.principal_id


# ---------------------------------------------------------------------------
# verify_chain
# ---------------------------------------------------------------------------


def test_verify_chain_empty_log_is_ok(temp_data_dir: Path) -> None:
    result = verify_chain()
    assert result.ok is True
    assert result.entries == 0
    assert result.head_hash == GENESIS_PREV_HASH
    assert result.broken_at_seq is None


def test_verify_chain_clean_log_is_ok(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")
    _make_charter(seed="c")
    result = verify_chain()
    assert result.ok is True
    assert result.entries == 3
    assert result.broken_at_seq is None


def test_verify_chain_detects_prev_hash_tampering(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")
    _make_charter(seed="c")

    # Rewrite the log: corrupt the prev_hash of seq=2.
    lines = log_file_path().read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = "sha256:" + "f" * 64
    lines[1] = json.dumps(second)
    log_file_path().write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain()
    assert result.ok is False
    assert result.broken_at_seq == 2
    assert result.reason is not None and "prev_hash mismatch" in result.reason


def test_verify_chain_detects_entry_hash_tampering(temp_data_dir: Path) -> None:
    _make_charter(seed="a")
    _make_charter(seed="b")

    # Change a non-hash field on seq=1 but leave its entry_hash alone —
    # recompute should disagree.
    lines = log_file_path().read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["binding"]["agent_id"] = "TAMPERED"
    lines[0] = json.dumps(first)
    log_file_path().write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain()
    assert result.ok is False
    assert result.broken_at_seq == 1
    assert result.reason is not None and "entry_hash mismatch" in result.reason


# ---------------------------------------------------------------------------
# sign_charter integration
# ---------------------------------------------------------------------------


def test_sign_charter_writes_one_log_entry(temp_data_dir: Path) -> None:
    """Issuing a Charter via the normal sign_charter path adds exactly one
    log entry."""
    _make_charter()
    assert len(list(read_log())) == 1


def test_re_signing_same_charter_id_is_idempotent(temp_data_dir: Path) -> None:
    """Revoke/renew re-sign a Charter whose `charter_id` is unchanged —
    the log MUST keep its original issuance entry and NOT add a duplicate.
    This is what makes the log a faithful record of issuance."""
    charter = _make_charter()
    original_entry = get_entry(charter.charter_id)
    assert original_entry is not None

    # Simulate a revoke re-sign: clear the signature, mutate lifecycle,
    # re-sign.
    private, public = generate_keypair()  # fresh key just to drive sign_charter
    # Use the ORIGINAL public key so the inline-vs-signature stays
    # consistent on the charter object.
    charter.provenance.issuer_signature = ""
    sign_charter(charter, private)

    entries = list(read_log())
    assert len(entries) == 1  # idempotent on charter_id
    assert entries[0].seq == original_entry.seq


def test_dataclass_round_trip(temp_data_dir: Path) -> None:
    """TransparencyEntry.to_dict / from_dict should round-trip exactly."""
    c = _make_charter()
    raw = json.loads(log_file_path().read_text(encoding="utf-8").strip())
    rebuilt = TransparencyEntry.from_dict(raw)
    assert rebuilt.to_dict() == raw
    assert rebuilt.charter_id == c.charter_id
