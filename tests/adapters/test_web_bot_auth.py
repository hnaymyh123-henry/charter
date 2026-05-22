"""Tests for `charter.adapters.web_bot_auth` (Issue #28 / A6)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from starlette.testclient import TestClient

from charter.adapters import _rfc9421, web_bot_auth
from charter.adapters.web_bot_auth import (
    WebBotAuthResult,
    sign_request,
    verify_request,
)
from charter.errors import JWKSNotFoundError
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
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _signed_charter() -> Charter:
    """Build a fully signed Charter with predictable clauses."""
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    c = Charter(
        charter_id="charter:alice@acme.com:research_agent_v1:2026-05-22",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Test"),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[
            Clause(id="C-001", type="scope", text="Read public data."),
            Clause(id="C-002", type="out_of_scope", text="Customer PII export."),
        ],
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
    sign_charter(c, private)
    return c


@pytest.fixture
def keypair_and_jwks() -> tuple[Any, Ed25519PublicKey, str, dict[str, dict[str, str]]]:
    """Return (private_key, public_key, key_id, jwks_dict)."""
    private, public = generate_keypair()
    public_key_str = public_key_to_string(public)
    key_id = kid_for_public_key(public_key_str)
    jwk = public_key_to_jwk(public_key_str, kid=key_id)
    return private, public, key_id, {key_id: jwk}


def _jwks_fetcher(jwks: dict[str, dict[str, str]]):
    def fetch(_origin: str) -> dict[str, dict[str, str]]:
        return jwks

    return fetch


# ---------------------------------------------------------------------------
# Reverse case 1: sign + verify roundtrip
# ---------------------------------------------------------------------------


def test_sign_then_verify_roundtrip(keypair_and_jwks):
    private, _public, key_id, jwks = keypair_and_jwks

    body = b'{"q": "hello"}'
    headers = {"content-type": "application/json"}

    signed = sign_request(
        "POST",
        "https://api.example.com/v1/search",
        headers,
        body,
        charter_url="https://charter.example.com/alice@acme.com/research_agent_v1",
        private_key=private,
        key_id=key_id,
    )

    # The three signature headers must be present.
    assert "Signature" in signed
    assert "Signature-Input" in signed
    assert "Content-Digest" in signed

    result = verify_request(
        "POST",
        "https://api.example.com/v1/search",
        signed,
        body,
        fetch_jwks_fn=_jwks_fetcher(jwks),
    )
    assert result.signature_ok is True
    assert result.charter_url == "https://charter.example.com/alice@acme.com/research_agent_v1"
    assert result.key_id == key_id
    assert result.reason == "ok"


def test_sign_empty_body_does_not_emit_content_digest(keypair_and_jwks):
    """RFC 9421 §2.1 allows omitting content-digest for empty bodies."""
    private, _public, key_id, jwks = keypair_and_jwks
    signed = sign_request(
        "GET",
        "https://api.example.com/healthz",
        {},
        b"",
        charter_url="https://charter.example.com/x/y",
        private_key=private,
        key_id=key_id,
    )
    assert "Content-Digest" not in signed

    result = verify_request(
        "GET",
        "https://api.example.com/healthz",
        signed,
        b"",
        fetch_jwks_fn=_jwks_fetcher(jwks),
    )
    assert result.signature_ok is True


# ---------------------------------------------------------------------------
# Reverse case 2: body tampering must be detected
# ---------------------------------------------------------------------------


def test_body_tampering_detected_via_content_digest(keypair_and_jwks):
    private, _public, key_id, jwks = keypair_and_jwks
    body = b'{"q": "hello"}'
    signed = sign_request(
        "POST",
        "https://api.example.com/v1/search",
        {},
        body,
        charter_url="https://charter.example.com/x/y",
        private_key=private,
        key_id=key_id,
    )

    tampered = b'{"q": "DELETE FROM users"}'
    result = verify_request(
        "POST",
        "https://api.example.com/v1/search",
        signed,
        tampered,
        fetch_jwks_fn=_jwks_fetcher(jwks),
    )
    assert result.signature_ok is False
    assert "signature did not verify" in result.reason


# ---------------------------------------------------------------------------
# Reverse case 3: missing charter_url parameter
# ---------------------------------------------------------------------------


def test_sign_request_rejects_empty_charter_url(keypair_and_jwks):
    private, _public, key_id, _jwks = keypair_and_jwks
    with pytest.raises(ValueError, match="charter_url is required"):
        sign_request(
            "GET",
            "https://api.example.com/",
            {},
            b"",
            charter_url="",
            private_key=private,
            key_id=key_id,
        )


def test_verify_request_rejects_missing_charter_url(keypair_and_jwks):
    """A Signature-Input that omits charter_url must be rejected."""
    private, _public, key_id, jwks = keypair_and_jwks

    # Hand-build a valid Ed25519 signature whose Signature-Input lacks charter_url.
    method = "GET"
    url = "https://api.example.com/healthz"
    covered = ["@method", "@path", "@authority"]
    params_value = (
        f"({' '.join(_rfc9421._sf_string(c) for c in covered)});"
        f'keyid="{key_id}";created=1700000000;alg="ed25519"'
    )
    base = _rfc9421._build_signature_base(
        method=method,
        url=url,
        headers_lower={},
        covered_components=covered,
        signature_params_value=params_value,
    )
    sig = private.sign(base)
    headers = {
        "Signature-Input": f"charter={params_value}",
        "Signature": f"charter=:{base64.b64encode(sig).decode('ascii')}:",
    }

    result = verify_request(method, url, headers, b"", fetch_jwks_fn=_jwks_fetcher(jwks))
    assert result.signature_ok is False
    assert "charter_url" in result.reason


def test_verify_request_missing_signature_headers():
    result = verify_request("GET", "https://api.example.com/", {}, b"")
    assert result.signature_ok is False
    assert "missing Signature" in result.reason


# ---------------------------------------------------------------------------
# Reverse case 4: key_id not in JWKS
# ---------------------------------------------------------------------------


def test_verify_request_key_id_not_in_jwks(keypair_and_jwks):
    private, _public, key_id, _jwks = keypair_and_jwks

    # Sign with the real key but advertise a key_id that isn't in the JWKS.
    bogus_key_id = "0" * 16
    signed = sign_request(
        "GET",
        "https://api.example.com/",
        {},
        b"",
        charter_url="https://charter.example.com/x/y",
        private_key=private,
        key_id=bogus_key_id,
    )

    # JWKS only knows the real key.
    other_private, other_public = generate_keypair()
    other_pk_str = public_key_to_string(other_public)
    other_kid = kid_for_public_key(other_pk_str)
    other_jwks = {other_kid: public_key_to_jwk(other_pk_str, kid=other_kid)}

    result = verify_request(
        "GET",
        "https://api.example.com/",
        signed,
        b"",
        fetch_jwks_fn=_jwks_fetcher(other_jwks),
    )
    assert result.signature_ok is False
    assert "not present in JWKS" in result.reason
    assert result.key_id == bogus_key_id


def test_verify_request_jwks_unreachable(keypair_and_jwks):
    private, _public, key_id, _jwks = keypair_and_jwks
    signed = sign_request(
        "GET",
        "https://api.example.com/",
        {},
        b"",
        charter_url="https://charter.example.com/x/y",
        private_key=private,
        key_id=key_id,
    )

    def boom(_origin: str) -> dict[str, dict[str, str]]:
        raise JWKSNotFoundError("network unreachable")

    result = verify_request("GET", "https://api.example.com/", signed, b"", fetch_jwks_fn=boom)
    assert result.signature_ok is False
    assert "JWKS lookup failed" in result.reason


# ---------------------------------------------------------------------------
# Reverse case 5: unsupported algorithm
# ---------------------------------------------------------------------------


def test_verify_request_rejects_non_ed25519_alg(keypair_and_jwks):
    """ADR-002: only Ed25519 is accepted, even if alg is just spoofed."""
    private, _public, key_id, jwks = keypair_and_jwks

    # Build a Signature-Input that claims alg=rsa-pss-sha512.
    covered = ["@method", "@path", "@authority"]
    params_value = (
        f"({' '.join(_rfc9421._sf_string(c) for c in covered)});"
        f'keyid="{key_id}";created=1700000000;alg="rsa-pss-sha512";'
        f'charter_url="https://charter.example.com/x/y"'
    )
    base = _rfc9421._build_signature_base(
        method="GET",
        url="https://api.example.com/",
        headers_lower={},
        covered_components=covered,
        signature_params_value=params_value,
    )
    sig = private.sign(base)
    headers = {
        "Signature-Input": f"charter={params_value}",
        "Signature": f"charter=:{base64.b64encode(sig).decode('ascii')}:",
    }

    result = verify_request(
        "GET", "https://api.example.com/", headers, b"", fetch_jwks_fn=_jwks_fetcher(jwks)
    )
    assert result.signature_ok is False
    assert "ed25519" in result.reason.lower()


# ---------------------------------------------------------------------------
# Middleware integration: happy path + 4xx paths
# ---------------------------------------------------------------------------


def _build_app(charter: Charter, jwks: dict[str, dict[str, str]], hits_grader=None) -> FastAPI:
    """Build a minimal FastAPI app with the gated middleware installed."""
    app = FastAPI()

    @app.get("/protected")
    def protected() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/protected")
    def protected_post(payload: dict[str, Any]) -> dict[str, Any]:
        return {"echo": payload}

    def fetch_charter(_url: str) -> Charter:
        return charter

    app.add_middleware(
        web_bot_auth._WebBotAuthMiddleware,
        fetch_charter_fn=fetch_charter,
        fetch_jwks_fn=_jwks_fetcher(jwks),
        hits_grader=hits_grader,
        task_from=None,
        cache_size=8,
    )
    return app


def test_middleware_happy_path_allow(keypair_and_jwks):
    private, _public, key_id, jwks = keypair_and_jwks
    charter = _signed_charter()

    def grader(_c: Charter, _task: str) -> list[dict[str, Any]]:
        # Hit C-001 (scope) so aggregate_verdict resolves to allow.
        return [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "in scope"}]

    app = _build_app(charter, jwks, hits_grader=grader)
    client = TestClient(app)

    url = "http://testserver/protected"
    signed = sign_request(
        "GET",
        url,
        {},
        b"",
        charter_url="https://charter.example.com/alice@acme.com/research_agent_v1",
        private_key=private,
        key_id=key_id,
    )
    resp = client.get("/protected", headers=signed)
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}


def test_middleware_rejects_missing_signature(keypair_and_jwks):
    _private, _public, _key_id, jwks = keypair_and_jwks
    charter = _signed_charter()
    app = _build_app(charter, jwks)
    client = TestClient(app)

    resp = client.get("/protected")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "web_bot_auth_signature_invalid"
    assert body["charter_url"] is None


def test_middleware_rejects_bad_signature(keypair_and_jwks):
    """Tamper with the body after signing — content-digest must catch it."""
    private, _public, key_id, jwks = keypair_and_jwks
    charter = _signed_charter()
    app = _build_app(charter, jwks)
    client = TestClient(app)

    url = "http://testserver/protected"
    body = b'{"safe": true}'
    signed = sign_request(
        "POST",
        url,
        {"content-type": "application/json"},
        body,
        charter_url="https://charter.example.com/alice@acme.com/research_agent_v1",
        private_key=private,
        key_id=key_id,
    )
    # Send a different body than what we signed.
    resp = client.post(
        "/protected",
        headers={**signed, "content-type": "application/json"},
        content=b'{"safe": false, "extra": "evil"}',
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "web_bot_auth_signature_invalid"


def test_middleware_rejects_incompatible_verdict(keypair_and_jwks):
    private, _public, key_id, jwks = keypair_and_jwks
    charter = _signed_charter()

    def grader(_c: Charter, _task: str) -> list[dict[str, Any]]:
        # Hit C-002 (out_of_scope) so aggregate_verdict resolves to incompatible.
        return [{"id": "C-002", "hit": True, "confidence": 0.95, "reason": "PII"}]

    app = _build_app(charter, jwks, hits_grader=grader)
    client = TestClient(app)

    url = "http://testserver/protected"
    signed = sign_request(
        "GET",
        url,
        {},
        b"",
        charter_url="https://charter.example.com/alice@acme.com/research_agent_v1",
        private_key=private,
        key_id=key_id,
    )
    resp = client.get("/protected", headers=signed)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "charter_incompatible"
    assert body["decision"] == "incompatible"
    assert body["applied_clauses"] == ["C-002"]
    assert body["charter_url"].endswith("research_agent_v1")


def test_middleware_rejects_needs_approval_default_grader(keypair_and_jwks):
    """Without a grader, no clauses hit -> verdict is needs_approval -> 403."""
    private, _public, key_id, jwks = keypair_and_jwks
    charter = _signed_charter()
    app = _build_app(charter, jwks, hits_grader=None)
    client = TestClient(app)

    url = "http://testserver/protected"
    signed = sign_request(
        "GET",
        url,
        {},
        b"",
        charter_url="https://charter.example.com/alice@acme.com/research_agent_v1",
        private_key=private,
        key_id=key_id,
    )
    resp = client.get("/protected", headers=signed)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "charter_needs_approval"


def test_middleware_cache_avoids_refetching_within_ttl(keypair_and_jwks):
    """Two requests against the same charter_url -> fetch_charter_fn called once."""
    private, _public, key_id, jwks = keypair_and_jwks
    charter = _signed_charter()
    app = FastAPI()

    @app.get("/protected")
    def protected() -> dict[str, str]:
        return {"ok": "yes"}

    call_count = {"n": 0}

    def fetch_charter(_url: str) -> Charter:
        call_count["n"] += 1
        return charter

    def grader(_c: Charter, _task: str) -> list[dict[str, Any]]:
        return [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "ok"}]

    app.add_middleware(
        web_bot_auth._WebBotAuthMiddleware,
        fetch_charter_fn=fetch_charter,
        fetch_jwks_fn=_jwks_fetcher(jwks),
        hits_grader=grader,
        task_from=None,
        cache_size=8,
    )
    client = TestClient(app)

    url = "http://testserver/protected"
    for _ in range(3):
        signed = sign_request(
            "GET",
            url,
            {},
            b"",
            charter_url="https://charter.example.com/alice@acme.com/research_agent_v1",
            private_key=private,
            key_id=key_id,
        )
        resp = client.get("/protected", headers=signed)
        assert resp.status_code == 200

    assert call_count["n"] == 1


def test_result_dataclass_is_immutable():
    """WebBotAuthResult is frozen so callers can't accidentally mutate it."""
    from dataclasses import FrozenInstanceError

    r = WebBotAuthResult(signature_ok=True, charter_url="x", key_id="y", reason="ok")
    with pytest.raises(FrozenInstanceError):
        r.signature_ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Low-level RFC 9421 subset sanity tests
# ---------------------------------------------------------------------------


def test_content_digest_value_shape():
    """Content-Digest must be `sha-256=:<base64>:`."""
    digest = _rfc9421.compute_content_digest(b"hello")
    assert digest.startswith("sha-256=:")
    assert digest.endswith(":")


def test_signature_input_parser_roundtrip():
    raw = (
        'charter=("@method" "@path" "@authority" "content-digest");'
        'keyid="abc"; created=1700000000; alg="ed25519"; '
        'charter_url="https://charter.example.com/x/y"'
    )
    label, covered, params = _rfc9421.parse_signature_input(raw)
    assert label == "charter"
    assert covered == ["@method", "@path", "@authority", "content-digest"]
    assert params["keyid"] == "abc"
    assert params["alg"] == "ed25519"
    assert params["charter_url"] == "https://charter.example.com/x/y"
    assert params["created"] == "1700000000"


def test_signature_input_parser_rejects_malformed():
    with pytest.raises(_rfc9421.SignatureInputParseError):
        _rfc9421.parse_signature_input("no equals sign here")
    with pytest.raises(_rfc9421.SignatureInputParseError):
        _rfc9421.parse_signature_input("label=no_paren")
