"""Tests for v0.8 #3: key-fingerprint pinning.

Covers:
  - `fingerprint_of` stable, content-only, sha256 hex.
  - `record_pin` / `get_pin` round-trip via JSON file.
  - `update_last_verified` bumps `last_verified` without touching `first_seen`.
  - `reset_pin` removes a pin and is idempotent.
  - `list_pins` returns the table.
  - `_fetch_and_verify` records pin on first fetch (TOFU).
  - `_fetch_and_verify` accepts matching key on second fetch.
  - `_fetch_and_verify` raises `CharterPinMismatchError` on key swap.
  - `charter pins list` and `charter pins reset` CLI commands.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from charter import keys as keys_mod
from charter.cli import cli
from charter.errors import CharterPinMismatchError
from charter.mcp_server import _fetch_and_verify
from charter.pins import (
    Pin,
    fingerprint_of,
    get_pin,
    list_pins,
    record_pin,
    reset_pin,
    update_last_verified,
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
from charter.signing import (
    generate_keypair,
    public_key_to_jwk,
    public_key_to_string,
    sign_charter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    keys_mod.clear_cache()
    yield
    keys_mod.clear_cache()


def _signed_charter(
    *,
    principal_id: str = "alice@acme.com",
    keypair: tuple | None = None,
) -> Charter:
    private, public = keypair if keypair is not None else generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id=f"charter:{principal_id}:agent_x:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id="agent_x"),
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


def _stub_fetch(monkeypatch, *, charter: Charter) -> None:
    """Route Charter fetch + matching JWKS so the pin check is the only
    interesting step. Clears the JWKS cache so tests that swap the JWKS
    body mid-test (e.g. key-rotation simulation) see the new body."""
    keys_mod.clear_cache()
    payload = charter.model_dump(mode="json")
    kid = charter.provenance.issuer_kid
    jwks_body: dict
    if kid is not None:
        jwks_body = {"keys": [public_key_to_jwk(charter.provenance.issuer_public_key, kid=kid)]}
    else:
        jwks_body = {"keys": []}

    def fake_get(url, *, timeout=10.0):  # noqa: ARG001
        if "/.well-known/jwks.json" in url:
            req = httpx.Request("GET", url)
            return httpx.Response(200, json=jwks_body, request=req)
        req = httpx.Request("GET", "http://test")
        return httpx.Response(200, json=payload, request=req)

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)
    monkeypatch.setattr("charter.keys.httpx.get", fake_get)


# ---------------------------------------------------------------------------
# fingerprint_of
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_for_same_key() -> None:
    _, public = generate_keypair()
    s = public_key_to_string(public)
    assert fingerprint_of(s) == fingerprint_of(s)


def test_fingerprint_differs_for_different_keys() -> None:
    _, p1 = generate_keypair()
    _, p2 = generate_keypair()
    assert fingerprint_of(public_key_to_string(p1)) != fingerprint_of(public_key_to_string(p2))


def test_fingerprint_format_is_sha256_hex() -> None:
    _, public = generate_keypair()
    fp = fingerprint_of(public_key_to_string(public))
    assert fp.startswith("sha256:")
    digest = fp.removeprefix("sha256:")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_fingerprint_rejects_non_ed25519_prefix() -> None:
    with pytest.raises(ValueError, match="ed25519"):
        fingerprint_of("rsa:abc123")


# ---------------------------------------------------------------------------
# pin file round-trips
# ---------------------------------------------------------------------------


def test_record_and_get_pin_round_trip(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    fp = fingerprint_of(public_key_to_string(public))

    assert get_pin("alice@acme.com") is None
    written = record_pin("alice@acme.com", fp)
    assert isinstance(written, Pin)

    read_back = get_pin("alice@acme.com")
    assert read_back is not None
    assert read_back.fingerprint == fp
    assert read_back.first_seen == written.first_seen
    assert read_back.last_verified == written.last_verified


def test_list_pins_returns_all(temp_data_dir: Path) -> None:
    _, p1 = generate_keypair()
    _, p2 = generate_keypair()
    record_pin("alice@acme.com", fingerprint_of(public_key_to_string(p1)))
    record_pin("bob@startup.io", fingerprint_of(public_key_to_string(p2)))

    table = list_pins()
    assert set(table.keys()) == {"alice@acme.com", "bob@startup.io"}


def test_update_last_verified_bumps_only_that_field(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    fp = fingerprint_of(public_key_to_string(public))
    initial = record_pin("alice@acme.com", fp)

    # Sleep just long enough for the second-precision timestamp to advance.
    time.sleep(1.1)
    update_last_verified("alice@acme.com")

    after = get_pin("alice@acme.com")
    assert after is not None
    assert after.first_seen == initial.first_seen
    assert after.last_verified > initial.last_verified
    assert after.fingerprint == fp


def test_reset_pin_drops_entry(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    record_pin("alice@acme.com", fingerprint_of(public_key_to_string(public)))

    assert reset_pin("alice@acme.com") is True
    assert get_pin("alice@acme.com") is None
    # Idempotent: dropping a missing pin is a no-op returning False.
    assert reset_pin("alice@acme.com") is False


def test_pin_file_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`CHARTER_PIN_FILE` relocates pins.json without touching CHARTER_DATA_DIR."""
    custom = tmp_path / "elsewhere" / "pins.json"
    monkeypatch.setenv("CHARTER_PIN_FILE", str(custom))

    _, public = generate_keypair()
    record_pin("alice@acme.com", fingerprint_of(public_key_to_string(public)))

    assert custom.exists()
    assert get_pin("alice@acme.com") is not None


# ---------------------------------------------------------------------------
# _fetch_and_verify pin integration
# ---------------------------------------------------------------------------


def test_first_fetch_records_pin(temp_data_dir: Path, monkeypatch) -> None:
    charter = _signed_charter()
    _stub_fetch(monkeypatch, charter=charter)

    assert get_pin("alice@acme.com") is None
    _fetch_and_verify("http://test/x")

    pin = get_pin("alice@acme.com")
    assert pin is not None
    assert pin.fingerprint == fingerprint_of(charter.provenance.issuer_public_key)


def test_second_fetch_with_matching_key_updates_last_verified(
    temp_data_dir: Path, monkeypatch
) -> None:
    charter = _signed_charter()
    _stub_fetch(monkeypatch, charter=charter)

    _fetch_and_verify("http://test/x")
    initial = get_pin("alice@acme.com")
    assert initial is not None

    time.sleep(1.1)
    _fetch_and_verify("http://test/x")

    after = get_pin("alice@acme.com")
    assert after is not None
    assert after.first_seen == initial.first_seen
    assert after.last_verified > initial.last_verified


def test_key_swap_raises_pin_mismatch(temp_data_dir: Path, monkeypatch) -> None:
    """First fetch pins key A. Second fetch presents key B for the same
    principal_id (i.e. attacker forged a fresh Charter with their own key
    + a matching JWKS). The pin check must catch it."""
    original = _signed_charter()
    _stub_fetch(monkeypatch, charter=original)
    _fetch_and_verify("http://test/x")

    # Forge a fresh Charter for the SAME principal but signed with a
    # different key. The forger published their own JWKS and inline key.
    forged = _signed_charter()  # different keypair under the hood
    _stub_fetch(monkeypatch, charter=forged)

    with pytest.raises(CharterPinMismatchError, match="Pinned fingerprint"):
        _fetch_and_verify("http://test/x")


def test_pin_check_isolated_per_principal(temp_data_dir: Path, monkeypatch) -> None:
    """Pinning alice doesn't affect bob — they have independent pins."""
    alice = _signed_charter(principal_id="alice@acme.com")
    bob = _signed_charter(principal_id="bob@startup.io")

    _stub_fetch(monkeypatch, charter=alice)
    _fetch_and_verify("http://test/alice")

    _stub_fetch(monkeypatch, charter=bob)
    _fetch_and_verify("http://test/bob")

    assert get_pin("alice@acme.com") is not None
    assert get_pin("bob@startup.io") is not None


# ---------------------------------------------------------------------------
# CLI: charter pins list / reset
# ---------------------------------------------------------------------------


def test_cli_pins_list_empty(temp_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["pins", "list"])
    assert result.exit_code == 0
    assert "No pins recorded yet." in result.output


def test_cli_pins_list_shows_entries(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    record_pin("alice@acme.com", fingerprint_of(public_key_to_string(public)))

    runner = CliRunner()
    result = runner.invoke(cli, ["pins", "list"])
    assert result.exit_code == 0
    assert "alice@acme.com" in result.output
    assert "sha256:" in result.output


def test_cli_pins_reset_with_yes_flag(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    record_pin("alice@acme.com", fingerprint_of(public_key_to_string(public)))

    runner = CliRunner()
    result = runner.invoke(cli, ["pins", "reset", "alice@acme.com", "--yes"])
    assert result.exit_code == 0
    assert "Pin dropped" in result.output
    assert get_pin("alice@acme.com") is None


def test_cli_pins_reset_prompts_without_yes(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    record_pin("alice@acme.com", fingerprint_of(public_key_to_string(public)))

    runner = CliRunner()
    # Decline at the prompt.
    result = runner.invoke(cli, ["pins", "reset", "alice@acme.com"], input="n\n")
    assert result.exit_code != 0  # Click `abort=True` -> non-zero exit
    assert get_pin("alice@acme.com") is not None  # still pinned


def test_cli_pins_reset_no_such_pin(temp_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["pins", "reset", "nobody@nowhere.io", "--yes"])
    assert result.exit_code == 1
    assert "No pin recorded" in result.output
