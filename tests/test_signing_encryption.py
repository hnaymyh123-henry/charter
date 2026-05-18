"""Tests for `charter.signing.save_private_key` / `load_private_key`
with the CHARTER_KEY_PASSPHRASE encryption path."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from charter.signing import (
    generate_keypair,
    load_private_key,
    save_private_key,
)


def _roundtrip_sample_message(private_key, expected_class) -> None:
    """Quick sanity check that the reloaded key actually signs."""
    sig = private_key.sign(b"hello")
    private_key.public_key().verify(sig, b"hello")
    assert isinstance(private_key, expected_class)


# ---------------------------------------------------------------------------
# Encrypted round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_with_passphrase_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_KEY_PASSPHRASE", "correct horse battery staple")
    private, _ = generate_keypair()
    path = tmp_path / "alice.pem"
    save_private_key(private, path)

    # File on disk should NOT contain the unencrypted PRIVATE KEY header.
    contents = path.read_text()
    assert "BEGIN ENCRYPTED PRIVATE KEY" in contents
    assert "BEGIN PRIVATE KEY-----" not in contents

    reloaded = load_private_key(path)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _roundtrip_sample_message(reloaded, Ed25519PrivateKey)


def test_wrong_passphrase_fails_to_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTER_KEY_PASSPHRASE", "correct")
    private, _ = generate_keypair()
    path = tmp_path / "alice.pem"
    save_private_key(private, path)

    monkeypatch.setenv("CHARTER_KEY_PASSPHRASE", "wrong")
    with pytest.raises(ValueError):
        # With a wrong passphrase AND encrypted-on-disk PEM, both branches
        # of load_private_key fail (encrypted needs a password, fallback
        # also fails). The fallback raises TypeError which propagates as
        # ValueError after cryptography's wrap.
        load_private_key(path)


# ---------------------------------------------------------------------------
# Plaintext path + WARN log
# ---------------------------------------------------------------------------


def test_save_without_passphrase_writes_plaintext_pem_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("CHARTER_KEY_PASSPHRASE", raising=False)
    private, _ = generate_keypair()
    path = tmp_path / "alice.pem"

    with caplog.at_level(logging.WARNING, logger="charter.signing"):
        save_private_key(private, path)

    contents = path.read_text()
    assert "BEGIN PRIVATE KEY-----" in contents
    assert "BEGIN ENCRYPTED PRIVATE KEY" not in contents

    # Loud WARN log so production deployers can't miss it.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unencrypted" in r.message for r in warnings)


def test_load_without_passphrase_when_disk_is_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHARTER_KEY_PASSPHRASE", raising=False)
    private, _ = generate_keypair()
    path = tmp_path / "alice.pem"
    save_private_key(private, path)

    reloaded = load_private_key(path)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _roundtrip_sample_message(reloaded, Ed25519PrivateKey)


# ---------------------------------------------------------------------------
# Backward compatibility: v0 plaintext PEMs remain loadable when a
# passphrase is later configured
# ---------------------------------------------------------------------------


def test_loads_legacy_plaintext_when_passphrase_now_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Step 1: write the key with NO passphrase (simulating a v0 deploy).
    monkeypatch.delenv("CHARTER_KEY_PASSPHRASE", raising=False)
    private, _ = generate_keypair()
    path = tmp_path / "alice.pem"
    save_private_key(private, path)

    # Step 2: a later deploy adds CHARTER_KEY_PASSPHRASE. The legacy
    # plaintext file should still load — load_private_key tries the
    # passphrase, fails, and silently falls back to no-passphrase.
    monkeypatch.setenv("CHARTER_KEY_PASSPHRASE", "newly-configured")
    with caplog.at_level(logging.INFO, logger="charter.signing"):
        reloaded = load_private_key(path)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _roundtrip_sample_message(reloaded, Ed25519PrivateKey)

    # An INFO-level log records the fallback so deployers know they
    # should re-save the key under the new passphrase.
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("plaintext_fallback" in str(getattr(r, "outcome", "")) for r in infos)


# ---------------------------------------------------------------------------
# Whitespace-only passphrase is treated as unset
# ---------------------------------------------------------------------------


def test_whitespace_passphrase_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_KEY_PASSPHRASE", "    ")
    private, _ = generate_keypair()
    path = tmp_path / "alice.pem"
    save_private_key(private, path)

    contents = path.read_text()
    # Whitespace-only env -> treated as unset -> plaintext.
    assert "BEGIN PRIVATE KEY-----" in contents
    assert "BEGIN ENCRYPTED PRIVATE KEY" not in contents
