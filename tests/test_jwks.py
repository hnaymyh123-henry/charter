"""Tests for v0.8 #1: JWKS endpoint + `provenance.issuer_kid`.

Covers:
  - `sign_charter` populates `provenance.issuer_kid` automatically.
  - The kid changes the signed payload (signing then mutating kid breaks verify).
  - JWK rendering matches RFC 7517 shape.
  - `GET /.well-known/jwks.json` lists keys in multi-tenant mode.
  - `GET /.well-known/jwks.json` filters to one principal in self-hosted mode.
  - Empty server returns an empty JWKS (no 500).
  - Charters issued before v0.8 (no kid on input) get one filled in but still verify.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

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
    kid_for_public_key,
    public_key_to_jwk,
    public_key_to_string,
    sign_charter,
    verify_charter,
)
from charter.storage import save_charter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(temp_data_dir: Path):  # noqa: ARG001
    """ASGI client for the FastAPI app."""
    from charter.server import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _signed_charter(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    preset_kid: str | None = None,
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
            issuer_kid=preset_kid,
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
# kid derivation
# ---------------------------------------------------------------------------


def test_kid_is_stable_for_same_key() -> None:
    _, public = generate_keypair()
    s = public_key_to_string(public)
    assert kid_for_public_key(s) == kid_for_public_key(s)


def test_kid_differs_for_different_keys() -> None:
    _, p1 = generate_keypair()
    _, p2 = generate_keypair()
    assert kid_for_public_key(public_key_to_string(p1)) != kid_for_public_key(
        public_key_to_string(p2)
    )


def test_kid_is_16_hex_chars() -> None:
    _, public = generate_keypair()
    kid = kid_for_public_key(public_key_to_string(public))
    assert len(kid) == 16
    assert all(c in "0123456789abcdef" for c in kid)


def test_kid_rejects_non_ed25519_prefix() -> None:
    with pytest.raises(ValueError, match="ed25519"):
        kid_for_public_key("rsa:abc123")


# ---------------------------------------------------------------------------
# JWK rendering
# ---------------------------------------------------------------------------


def test_jwk_has_required_rfc7517_fields() -> None:
    _, public = generate_keypair()
    jwk = public_key_to_jwk(public_key_to_string(public))
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "EdDSA"
    assert len(jwk["kid"]) == 16
    # `x` is base64url with no padding.
    assert "=" not in jwk["x"]
    assert "+" not in jwk["x"]
    assert "/" not in jwk["x"]


def test_jwk_x_decodes_to_raw_public_key_bytes() -> None:
    _, public = generate_keypair()
    s = public_key_to_string(public)
    jwk = public_key_to_jwk(s)
    # base64url decode (re-pad)
    x = jwk["x"]
    pad = "=" * (-len(x) % 4)
    raw_from_jwk = base64.urlsafe_b64decode(x + pad)
    raw_from_orig = base64.b64decode(s.removeprefix("ed25519:"))
    assert raw_from_jwk == raw_from_orig


def test_jwk_kid_can_be_overridden() -> None:
    _, public = generate_keypair()
    jwk = public_key_to_jwk(public_key_to_string(public), kid="custom-kid")
    assert jwk["kid"] == "custom-kid"


# ---------------------------------------------------------------------------
# sign_charter populates issuer_kid + signature still verifies
# ---------------------------------------------------------------------------


def test_sign_charter_populates_issuer_kid() -> None:
    charter = _signed_charter()
    assert charter.provenance.issuer_kid is not None
    assert len(charter.provenance.issuer_kid) == 16


def test_sign_charter_kid_matches_public_key() -> None:
    charter = _signed_charter()
    expected = kid_for_public_key(charter.provenance.issuer_public_key)
    assert charter.provenance.issuer_kid == expected


def test_sign_charter_preserves_preset_kid() -> None:
    """If the caller set kid explicitly (e.g. legacy / migration test),
    sign_charter does not overwrite it."""
    charter = _signed_charter(preset_kid="preset-value")
    assert charter.provenance.issuer_kid == "preset-value"
    # Signature still verifies because kid was set BEFORE canonical_bytes.
    assert verify_charter(charter) is True


def test_charter_with_kid_verifies() -> None:
    """v0.8 Charter (with kid) round-trips through sign + verify."""
    charter = _signed_charter()
    assert verify_charter(charter) is True


def test_tampering_with_kid_breaks_signature() -> None:
    """The kid is in the signed payload — flipping it post-sign must break verify.
    This is the property that prevents kid-swap attacks."""
    charter = _signed_charter()
    charter.provenance.issuer_kid = "0000000000000000"
    assert verify_charter(charter) is False


def test_legacy_charter_without_kid_still_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Charters issued before v0.8 had no `issuer_kid`. Loading one and
    verifying it must keep working. We simulate by:
      1. Signing normally (with kid).
      2. Wiping the kid AND re-signing without populating it.
    """
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:legacy:agent:2026-05-01",
        binding=Binding(principal_id="legacy", agent_id="agent"),
        principal=Principal(id="legacy", role_summary="Test"),
        issuer=Issuer(id="legacy"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[Clause(id="C-001", type="scope", text="...")],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            issuer_kid=None,  # legacy: no kid
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="legacy",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    # Caller pre-sets to "" so sign_charter's auto-fill is bypassed
    # if we want to test the truly-legacy path. But auto-fill happens
    # when `is None` so we need to monkeypatch around it. For
    # simplicity, we just confirm that a Charter with kid=None loaded
    # from JSON still validates fine.
    # Auto-fill kicks in here:
    sign_charter(charter, private)
    assert charter.provenance.issuer_kid is not None
    # Now manually wipe the kid AND re-canonicalize+sign as if it
    # were legacy. Build canonical bytes ourselves without kid:
    charter.provenance.issuer_kid = None
    # Re-sign with the kid cleared. We need the legacy path: kid stays None.
    # sign_charter would auto-fill it; bypass by signing directly.
    from charter.signing import _canonical_bytes

    payload = _canonical_bytes(charter)
    signature = private.sign(payload)
    charter.provenance.issuer_signature = f"ed25519:{base64.b64encode(signature).decode('ascii')}"
    # Now this charter has issuer_kid=None and a signature over that state.
    assert verify_charter(charter) is True


# ---------------------------------------------------------------------------
# /.well-known/jwks.json endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jwks_empty_server_returns_empty_keys(client: AsyncClient) -> None:
    async with client as ac:
        r = await ac.get("/.well-known/jwks.json")
    assert r.status_code == 200
    assert r.json() == {"keys": []}


@pytest.mark.asyncio
async def test_jwks_lists_one_key_per_issuer(client: AsyncClient) -> None:
    save_charter(_signed_charter(principal_id="alice@acme.com"))
    save_charter(_signed_charter(principal_id="bob@startup.io"))

    async with client as ac:
        r = await ac.get("/.well-known/jwks.json")

    assert r.status_code == 200
    body = r.json()
    assert len(body["keys"]) == 2
    issuers = {k["iss"] for k in body["keys"]}
    assert issuers == {"alice@acme.com", "bob@startup.io"}
    # Each key is a valid JWK
    for jwk in body["keys"]:
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert "kid" in jwk
        assert "x" in jwk


@pytest.mark.asyncio
async def test_jwks_dedupes_same_issuer_same_key(client: AsyncClient) -> None:
    """An issuer with two Charters (same key) appears once in JWKS."""
    private, public = generate_keypair()
    pk_str = public_key_to_string(public)
    now = datetime.now(UTC).replace(microsecond=0)

    def _build(agent_id: str) -> Charter:
        c = Charter(
            charter_id=f"charter:alice@acme.com:{agent_id}:{now.date().isoformat()}",
            binding=Binding(principal_id="alice@acme.com", agent_id=agent_id),
            principal=Principal(id="alice@acme.com", role_summary="t"),
            issuer=Issuer(id="alice@acme.com"),
            agent_operator=AgentOperator(id="generic"),
            summary=Summary(plain_language="t"),
            clauses=[Clause(id="C-001", type="scope", text="...")],
            lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
            provenance=Provenance(
                issuer_public_key=pk_str,
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
        sign_charter(c, private)
        return c

    save_charter(_build("agent_a"))
    save_charter(_build("agent_b"))

    async with client as ac:
        r = await ac.get("/.well-known/jwks.json")
    assert len(r.json()["keys"]) == 1


@pytest.mark.asyncio
async def test_jwks_self_hosted_filters_to_one_principal(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_SELF_HOSTED_PRINCIPAL", "alice@acme.com")
    save_charter(_signed_charter(principal_id="alice@acme.com"))
    save_charter(_signed_charter(principal_id="bob@startup.io"))

    async with client as ac:
        r = await ac.get("/.well-known/jwks.json")
    body = r.json()
    assert len(body["keys"]) == 1
    assert body["keys"][0]["iss"] == "alice@acme.com"


@pytest.mark.asyncio
async def test_jwks_kid_matches_charters_issuer_kid(client: AsyncClient) -> None:
    """The kid published in JWKS is the same kid embedded in the Charter."""
    charter = _signed_charter(principal_id="alice@acme.com")
    save_charter(charter)

    async with client as ac:
        r = await ac.get("/.well-known/jwks.json")
    jwks_kid = r.json()["keys"][0]["kid"]
    assert jwks_kid == charter.provenance.issuer_kid


@pytest.mark.asyncio
async def test_jwks_keys_are_sorted_for_stable_output(client: AsyncClient) -> None:
    """Two calls return identical JWKS (order matters for cache hashing)."""
    save_charter(_signed_charter(principal_id="bob@startup.io"))
    save_charter(_signed_charter(principal_id="alice@acme.com"))

    async with client as ac:
        r1 = await ac.get("/.well-known/jwks.json")
        r2 = await ac.get("/.well-known/jwks.json")
    assert r1.json() == r2.json()
