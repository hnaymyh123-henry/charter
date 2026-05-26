"""Tests for `charter.grants` — AdHocGrant storage + signing + lifecycle.

Covers the B2.5 step-up protocol's persistence layer (ADR-013). The
endpoint-level behaviour (`POST /step-up`, `GET /grants/{id}`) lives
in `tests/test_stepup_endpoint.py`; this file focuses on the module.

Reuses `tmp_path` + `CHARTER_DATA_DIR` to isolate every test from
real data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from charter.errors import (
    CharterGrantExpiredError,
    CharterGrantNotFoundError,
    CharterGrantSignatureError,
)
from charter.grants import (
    grant_path,
    grants_dir,
    load_grant,
    revoke_grant,
    save_grant,
    verify_grant,
)
from charter.schema import AdHocGrant
from charter.signing import (
    generate_keypair,
    public_key_to_string,
    sign_grant,
    verify_grant_signature,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


def _make_grant(
    *,
    ttl_seconds: int = 300,
    task: str = "Pay $500 to vendor for urgent invoice.",
    charter_url: str = "https://test/alice@acme.com/pay_agent_v1",
    issuer_kid: str = "kid-test-0000",
    granted_at: datetime | None = None,
) -> AdHocGrant:
    """Build an UNSIGNED AdHocGrant for tests."""
    now = granted_at or datetime.now(UTC).replace(microsecond=0)
    return AdHocGrant(
        grant_id=uuid.uuid4().hex,
        charter_url=charter_url,
        task=task,
        justification="Vendor needs payment today; one-time exception.",
        granted_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        issuer_kid=issuer_kid,
    )


def _sign(grant: AdHocGrant):
    private, public = generate_keypair()
    sign_grant(grant, private)
    return grant, public_key_to_string(public)


# ---------------------------------------------------------------------------
# Signing + verification roundtrip
# ---------------------------------------------------------------------------


def test_grant_sign_verify_roundtrip() -> None:
    """Sign a grant, then verify it with the issuer public key — passes."""
    grant = _make_grant()
    signed, pub_str = _sign(grant)
    assert signed.issuer_signature.startswith("ed25519:")
    assert verify_grant_signature(signed, pub_str) is True


def test_grant_verify_signature_against_wrong_key_fails() -> None:
    """Signing with key A, verifying with key B -> False."""
    grant = _make_grant()
    private_a, _ = generate_keypair()
    _, public_b = generate_keypair()
    sign_grant(grant, private_a)
    assert verify_grant_signature(grant, public_key_to_string(public_b)) is False


def test_grant_tamper_task_fails_signature() -> None:
    """Mutating `task` AFTER signing breaks the signature."""
    grant = _make_grant()
    signed, pub_str = _sign(grant)
    signed.task = "DRAIN THE TREASURY"  # post-sign tamper
    assert verify_grant_signature(signed, pub_str) is False


def test_grant_tamper_charter_url_fails_signature() -> None:
    """Mutating `charter_url` AFTER signing breaks the signature."""
    grant = _make_grant()
    signed, pub_str = _sign(grant)
    signed.charter_url = "https://attacker.example/other"
    assert verify_grant_signature(signed, pub_str) is False


def test_grant_tamper_expires_at_fails_signature() -> None:
    """Mutating `expires_at` AFTER signing breaks the signature (canonical
    bytes commit to the timestamp)."""
    grant = _make_grant()
    signed, pub_str = _sign(grant)
    signed.expires_at = signed.expires_at + timedelta(days=365)
    assert verify_grant_signature(signed, pub_str) is False


def test_verify_grant_helper_returns_true_for_fresh_grant() -> None:
    """`verify_grant(...)` is the convenience wrapper: signature OK + unexpired."""
    grant = _make_grant(ttl_seconds=300)
    signed, pub_str = _sign(grant)
    assert verify_grant(signed, pub_str) is True


def test_verify_grant_helper_returns_false_when_expired() -> None:
    """Already-expired grant -> verify_grant returns False (no exception)."""
    past = datetime.now(UTC) - timedelta(hours=2)
    grant = _make_grant(granted_at=past, ttl_seconds=300)
    signed, pub_str = _sign(grant)
    assert verify_grant(signed, pub_str) is False


# ---------------------------------------------------------------------------
# save + load roundtrip
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(temp_data_dir: Path) -> None:
    """Save a signed grant, load it back, fields match."""
    grant = _make_grant()
    signed, pub_str = _sign(grant)
    path = save_grant(signed)
    assert path.exists()
    assert path.parent == grants_dir()

    loaded = load_grant(signed.grant_id, pub_str)
    assert loaded.grant_id == signed.grant_id
    assert loaded.task == signed.task
    assert loaded.charter_url == signed.charter_url
    assert loaded.issuer_signature == signed.issuer_signature


def test_load_missing_grant_raises_not_found(temp_data_dir: Path) -> None:
    _, public = generate_keypair()
    with pytest.raises(CharterGrantNotFoundError):
        load_grant("does_not_exist", public_key_to_string(public))


def test_load_expired_grant_raises_expired(temp_data_dir: Path) -> None:
    """A grant whose expires_at is in the past raises CharterGrantExpiredError."""
    past = datetime.now(UTC) - timedelta(hours=1)
    grant = _make_grant(granted_at=past, ttl_seconds=60)
    signed, pub_str = _sign(grant)
    save_grant(signed)
    with pytest.raises(CharterGrantExpiredError):
        load_grant(signed.grant_id, pub_str)


def test_load_tampered_grant_raises_signature_error(temp_data_dir: Path) -> None:
    """Sign, save, then mutate the file content -> signature error on load."""
    grant = _make_grant()
    signed, pub_str = _sign(grant)
    save_grant(signed)
    # Mutate the JSON on disk.
    path = grant_path(signed.grant_id)
    text = path.read_text(encoding="utf-8")
    text = text.replace(signed.task, "tampered task")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CharterGrantSignatureError):
        load_grant(signed.grant_id, pub_str)


# ---------------------------------------------------------------------------
# Path safety — reusing `_safe()` allowlist
# ---------------------------------------------------------------------------


def test_path_traversal_attack_is_blocked(temp_data_dir: Path) -> None:
    """A grant_id like `../../../etc/grants/xxx` must not escape `data/grants/`.

    `_safe()` substitutes every disallowed char with `_`, so the file
    name lands at `data/grants/.._.._.._etc_grants_xxx.json`. The
    resolved path must remain under `grants_dir()` — the boundary
    check in `grant_path` is the second line of defense.
    """
    malicious = "../../../etc/grants/xxx"
    path = grant_path(malicious)
    # Path must live under grants_dir().
    root = grants_dir().resolve()
    assert path.resolve().is_relative_to(root)
    # And the file does not exist (no traversal landed somewhere real).
    assert not path.exists()
    # And a load attempt yields NotFound (no traversal escape).
    _, public = generate_keypair()
    with pytest.raises(CharterGrantNotFoundError):
        load_grant(malicious, public_key_to_string(public))


def test_path_safety_for_empty_id_raises_not_found(temp_data_dir: Path) -> None:
    """Empty `grant_id` is rejected by `_safe()` -> NotFound (no crash)."""
    _, public = generate_keypair()
    with pytest.raises(CharterGrantNotFoundError):
        load_grant("", public_key_to_string(public))


def test_path_safety_for_whitespace_id_raises_not_found(temp_data_dir: Path) -> None:
    """Whitespace-only `grant_id` -> NotFound."""
    _, public = generate_keypair()
    with pytest.raises(CharterGrantNotFoundError):
        load_grant("   ", public_key_to_string(public))


# ---------------------------------------------------------------------------
# TTL bounds (model-level invariants — ADR-013)
# ---------------------------------------------------------------------------


def test_grant_ttl_too_short_rejected_at_model_validation() -> None:
    """expires_at - granted_at < 60s -> pydantic ValidationError."""
    now = datetime.now(UTC).replace(microsecond=0)
    with pytest.raises(Exception) as exc_info:
        AdHocGrant(
            grant_id=uuid.uuid4().hex,
            charter_url="https://test/x/y",
            task="t",
            justification="j",
            granted_at=now,
            expires_at=now + timedelta(seconds=10),  # too short
            issuer_kid="k",
        )
    # Pydantic raises ValidationError; assert the message mentions the bound.
    assert ">= 60s" in str(exc_info.value)


def test_grant_ttl_too_long_rejected_at_model_validation() -> None:
    """expires_at - granted_at > 3600s -> pydantic ValidationError."""
    now = datetime.now(UTC).replace(microsecond=0)
    with pytest.raises(Exception) as exc_info:
        AdHocGrant(
            grant_id=uuid.uuid4().hex,
            charter_url="https://test/x/y",
            task="t",
            justification="j",
            granted_at=now,
            expires_at=now + timedelta(hours=2),  # too long
            issuer_kid="k",
        )
    assert "<= 3600s" in str(exc_info.value)


def test_grant_ttl_at_lower_bound_is_accepted() -> None:
    """Exactly 60s is allowed (inclusive bound)."""
    now = datetime.now(UTC).replace(microsecond=0)
    g = AdHocGrant(
        grant_id=uuid.uuid4().hex,
        charter_url="https://test/x/y",
        task="t",
        justification="j",
        granted_at=now,
        expires_at=now + timedelta(seconds=60),
        issuer_kid="k",
    )
    assert (g.expires_at - g.granted_at).total_seconds() == 60


def test_grant_ttl_at_upper_bound_is_accepted() -> None:
    """Exactly 3600s is allowed (inclusive bound)."""
    now = datetime.now(UTC).replace(microsecond=0)
    g = AdHocGrant(
        grant_id=uuid.uuid4().hex,
        charter_url="https://test/x/y",
        task="t",
        justification="j",
        granted_at=now,
        expires_at=now + timedelta(seconds=3600),
        issuer_kid="k",
    )
    assert (g.expires_at - g.granted_at).total_seconds() == 3600


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


def test_revoke_grant_deletes_file(temp_data_dir: Path) -> None:
    grant = _make_grant()
    signed, _pub_str = _sign(grant)
    path = save_grant(signed)
    assert path.exists()
    assert revoke_grant(signed.grant_id) is True
    assert not path.exists()


def test_revoke_missing_grant_is_idempotent(temp_data_dir: Path) -> None:
    """`revoke_grant` on a non-existent id returns False, does not raise."""
    assert revoke_grant("never_existed") is False
    # And calling again is still safe.
    assert revoke_grant("never_existed") is False


def test_revoke_grant_with_traversal_input_is_safe(temp_data_dir: Path) -> None:
    """A traversal id never deletes anything outside `grants/`."""
    # Put a file outside grants/ that we DO NOT want deleted.
    sentinel = temp_data_dir / "DO_NOT_DELETE.txt"
    sentinel.write_text("hands off", encoding="utf-8")
    # Revoke with traversal; the safe-mapped path stays under grants/.
    revoke_grant("../../../DO_NOT_DELETE.txt")
    # Sentinel still exists.
    assert sentinel.exists()
    assert sentinel.read_text(encoding="utf-8") == "hands off"
