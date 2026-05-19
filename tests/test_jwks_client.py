"""Tests for v0.8 #2: JWKS client + `_fetch_and_verify` integration.

Covers:
  - `fetch_jwks` HTTP success + cache hit within TTL.
  - `fetch_jwks` cache expiry (re-fetches after TTL).
  - `fetch_jwks` raises `JWKSNotFoundError` on 404 / network failure.
  - `fetch_jwks` raises `JWKSParseError` on malformed body.
  - `jwk_to_public_key_string` round-trips with `public_key_to_jwk`.
  - `_fetch_and_verify` with `kid` looks up the JWKS and verifies.
  - `_fetch_and_verify` raises `CharterKeyMismatchError` when:
        a) `kid` not listed in JWKS.
        b) JWKS key disagrees with inline `issuer_public_key`.
  - `_fetch_and_verify` propagates `JWKSNotFoundError` when JWKS is unreachable
    but Charter has a kid (strict trust).
  - Legacy Charter (no `kid`) skips JWKS check entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from charter import keys as keys_mod
from charter.errors import CharterKeyMismatchError, JWKSNotFoundError, JWKSParseError
from charter.keys import (
    fetch_jwks,
    issuer_origin_from_url,
    jwk_to_public_key_string,
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
from charter.signing import (
    generate_keypair,
    public_key_to_jwk,
    public_key_to_string,
    sign_charter,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    """Every test starts with an empty JWKS cache so monotonic-time leftovers
    from a prior test don't blur cache-hit vs cache-miss assertions."""
    keys_mod.clear_cache()
    yield
    keys_mod.clear_cache()


def _signed_charter(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    legacy: bool = False,
) -> tuple[Charter, str]:
    """Build a signed Charter. Returns `(charter, inline_public_key_str)`.

    `legacy=True` clears the auto-populated `issuer_kid` and re-signs the
    canonical payload without the kid field, simulating a v0.7 Charter.
    """
    private, public = generate_keypair()
    pk_str = public_key_to_string(public)
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
    sign_charter(charter, private)

    if legacy:
        import base64

        from charter.signing import _canonical_bytes

        charter.provenance.issuer_kid = None
        payload = _canonical_bytes(charter)
        sig = private.sign(payload)
        charter.provenance.issuer_signature = "ed25519:" + base64.b64encode(sig).decode("ascii")

    return charter, pk_str


class _StubResponse:
    """Minimal httpx.Response stand-in supporting `.raise_for_status()`,
    `.json()`, and the attributes our code reads on errors."""

    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body
        self.request = httpx.Request("GET", "http://stub")

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _stub_jwks_get(monkeypatch, *, responses: list[_StubResponse | Exception]):
    """Make the JWKS endpoint return one response per call. Each call pops
    the next entry; if it's an Exception, raise it; otherwise return it."""
    calls = {"n": 0}
    queue = list(responses)

    def fake_get(_url, *, timeout=10.0):  # noqa: ARG001
        calls["n"] += 1
        if not queue:
            raise AssertionError("fake_get called more times than scripted")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr("charter.keys.httpx.get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# issuer_origin_from_url
# ---------------------------------------------------------------------------


def test_issuer_origin_from_url_basic() -> None:
    assert issuer_origin_from_url("http://localhost:8000/alice/a") == "http://localhost:8000"
    assert issuer_origin_from_url("https://issuer.example.com/p/a") == "https://issuer.example.com"


def test_issuer_origin_from_url_rejects_bare_path() -> None:
    with pytest.raises(ValueError):
        issuer_origin_from_url("/just/a/path")


# ---------------------------------------------------------------------------
# fetch_jwks — happy path + caching
# ---------------------------------------------------------------------------


def test_fetch_jwks_returns_kid_indexed_map(monkeypatch) -> None:
    _, public = generate_keypair()
    jwk = public_key_to_jwk(public_key_to_string(public))
    _stub_jwks_get(
        monkeypatch,
        responses=[_StubResponse(200, {"keys": [jwk]})],
    )
    result = fetch_jwks("http://issuer.example.com")
    assert jwk["kid"] in result
    assert result[jwk["kid"]] == jwk


def test_fetch_jwks_caches_within_ttl(monkeypatch) -> None:
    _, public = generate_keypair()
    jwk = public_key_to_jwk(public_key_to_string(public))
    calls = _stub_jwks_get(
        monkeypatch,
        responses=[_StubResponse(200, {"keys": [jwk]})],
    )
    a = fetch_jwks("http://issuer.example.com")
    b = fetch_jwks("http://issuer.example.com")
    assert a == b
    assert calls["n"] == 1  # second call served from cache


def test_fetch_jwks_refetches_after_ttl_expiry(monkeypatch) -> None:
    _, public = generate_keypair()
    jwk = public_key_to_jwk(public_key_to_string(public))
    calls = _stub_jwks_get(
        monkeypatch,
        responses=[
            _StubResponse(200, {"keys": [jwk]}),
            _StubResponse(200, {"keys": [jwk]}),
        ],
    )

    # Force a tiny TTL and a controllable monotonic clock.
    monkeypatch.setenv("CHARTER_JWKS_CACHE_TTL", "1")
    clock = {"t": 100.0}
    monkeypatch.setattr("charter.keys.time.monotonic", lambda: clock["t"])

    fetch_jwks("http://issuer.example.com")
    assert calls["n"] == 1
    clock["t"] += 10.0  # 10s after fetch, well past 1s TTL
    fetch_jwks("http://issuer.example.com")
    assert calls["n"] == 2


def test_fetch_jwks_404_raises_not_found(monkeypatch) -> None:
    _stub_jwks_get(monkeypatch, responses=[_StubResponse(404, {"detail": "nope"})])
    with pytest.raises(JWKSNotFoundError):
        fetch_jwks("http://issuer.example.com")


def test_fetch_jwks_connection_error_raises_not_found(monkeypatch) -> None:
    _stub_jwks_get(
        monkeypatch,
        responses=[httpx.ConnectError("conn refused")],
    )
    with pytest.raises(JWKSNotFoundError):
        fetch_jwks("http://issuer.example.com")


def test_fetch_jwks_malformed_body_raises_parse_error(monkeypatch) -> None:
    _stub_jwks_get(monkeypatch, responses=[_StubResponse(200, {"oops": "no keys field"})])
    with pytest.raises(JWKSParseError):
        fetch_jwks("http://issuer.example.com")


def test_fetch_jwks_missing_kid_raises_parse_error(monkeypatch) -> None:
    _stub_jwks_get(
        monkeypatch,
        responses=[_StubResponse(200, {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "abc"}]})],
    )
    with pytest.raises(JWKSParseError):
        fetch_jwks("http://issuer.example.com")


# ---------------------------------------------------------------------------
# jwk_to_public_key_string
# ---------------------------------------------------------------------------


def test_jwk_round_trips_with_public_key_to_jwk() -> None:
    _, public = generate_keypair()
    s = public_key_to_string(public)
    jwk = public_key_to_jwk(s)
    assert jwk_to_public_key_string(jwk) == s


def test_jwk_to_public_key_string_rejects_non_ed25519() -> None:
    with pytest.raises(ValueError):
        jwk_to_public_key_string({"kty": "RSA", "crv": "Ed25519", "kid": "x", "x": "y"})


def test_jwk_to_public_key_string_rejects_missing_x() -> None:
    with pytest.raises(ValueError):
        jwk_to_public_key_string({"kty": "OKP", "crv": "Ed25519", "kid": "x"})


# ---------------------------------------------------------------------------
# _fetch_and_verify integration with JWKS
# ---------------------------------------------------------------------------


def _stub_charter_and_jwks(
    monkeypatch,
    *,
    charter_payload: dict,
    jwks_body: dict | Exception,
    jwks_status: int = 200,
    tmp_path: Path,
) -> None:
    """Route a single fake `httpx.get` by URL — `/.well-known/jwks.json`
    returns the JWKS body (or raises), anything else returns the Charter."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))

    def fake_get(url, *, timeout=10.0):  # noqa: ARG001
        if "/.well-known/jwks.json" in url:
            if isinstance(jwks_body, Exception):
                raise jwks_body
            return _StubResponse(jwks_status, jwks_body)
        req = httpx.Request("GET", url)
        return httpx.Response(200, json=charter_payload, request=req)

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)
    monkeypatch.setattr("charter.keys.httpx.get", fake_get)


def test_v08_charter_verifies_when_jwks_lists_matching_key(monkeypatch, tmp_path) -> None:
    charter, pk_str = _signed_charter()
    jwk = public_key_to_jwk(pk_str)
    _stub_charter_and_jwks(
        monkeypatch,
        charter_payload=charter.model_dump(mode="json"),
        jwks_body={"keys": [jwk]},
        tmp_path=tmp_path,
    )

    result = _fetch_and_verify("http://issuer.example.com/alice/agent")
    assert result.charter_id == charter.charter_id


def test_v08_charter_kid_not_in_jwks_raises_key_mismatch(monkeypatch, tmp_path) -> None:
    charter, _pk_str = _signed_charter()
    # JWKS has a totally different key
    _, other_public = generate_keypair()
    other_jwk = public_key_to_jwk(public_key_to_string(other_public))
    _stub_charter_and_jwks(
        monkeypatch,
        charter_payload=charter.model_dump(mode="json"),
        jwks_body={"keys": [other_jwk]},
        tmp_path=tmp_path,
    )

    with pytest.raises(CharterKeyMismatchError, match="not present in JWKS"):
        _fetch_and_verify("http://issuer.example.com/alice/agent")


def test_v08_charter_jwks_key_disagrees_with_inline_raises_key_mismatch(
    monkeypatch, tmp_path
) -> None:
    """JWKS publishes a key under the Charter's kid, but the bytes are
    different from the inline public_key. This catches the case where
    the JWKS is honest and the Charter is forged."""
    charter, _pk_str = _signed_charter()
    kid = charter.provenance.issuer_kid
    assert kid is not None

    # Build a JWK with the same kid but different `x`.
    _, other_public = generate_keypair()
    forged_jwk = public_key_to_jwk(public_key_to_string(other_public))
    forged_jwk["kid"] = kid

    _stub_charter_and_jwks(
        monkeypatch,
        charter_payload=charter.model_dump(mode="json"),
        jwks_body={"keys": [forged_jwk]},
        tmp_path=tmp_path,
    )

    with pytest.raises(CharterKeyMismatchError, match="disagrees with JWKS"):
        _fetch_and_verify("http://issuer.example.com/alice/agent")


def test_v08_charter_jwks_unreachable_propagates_not_found(monkeypatch, tmp_path) -> None:
    """When the Charter carries a kid and JWKS is unreachable, we fail
    strict — the issuer can't be cross-checked."""
    charter, _ = _signed_charter()
    _stub_charter_and_jwks(
        monkeypatch,
        charter_payload=charter.model_dump(mode="json"),
        jwks_body=httpx.ConnectError("conn refused"),
        tmp_path=tmp_path,
    )

    with pytest.raises(JWKSNotFoundError):
        _fetch_and_verify("http://issuer.example.com/alice/agent")


def test_legacy_charter_without_kid_skips_jwks(monkeypatch, tmp_path) -> None:
    """A Charter with `issuer_kid=None` should NOT trigger a JWKS fetch.
    We confirm by making JWKS raise — if the JWKS path were taken, the
    test would fail."""
    charter, _ = _signed_charter(legacy=True)
    assert charter.provenance.issuer_kid is None

    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))

    def fake_get(url, *, timeout=10.0):  # noqa: ARG001
        if "/.well-known/jwks.json" in url:
            raise AssertionError("JWKS must NOT be fetched for a legacy Charter")
        req = httpx.Request("GET", url)
        return httpx.Response(200, json=charter.model_dump(mode="json"), request=req)

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)
    monkeypatch.setattr("charter.keys.httpx.get", fake_get)

    result = _fetch_and_verify("http://issuer.example.com/alice/agent")
    assert result.charter_id == charter.charter_id
