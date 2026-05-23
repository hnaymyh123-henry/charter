"""Adversarial: cryptographic attacks against the v0.8 trust model.

Covers the three trust layers documented in ADR-007:

  - Layer 1 (signature): inline Ed25519 signature must verify against
    the embedded public key. `_canonical_bytes` covers everything except
    `issuer_signature` and `transparency_log_id`, so any post-sign
    mutation of a signed field breaks verification.

  - Layer 2 (JWKS cross-check): a Charter carrying `issuer_kid` must
    match a JWK published at the issuer origin's `/.well-known/jwks.json`,
    and that JWK must match the Charter's inline `issuer_public_key`.

  - Layer 3 (key fingerprint pinning): after the first fetch we pin the
    fingerprint of the issuer's verifying key. Subsequent fetches that
    present a different key for the same `principal_id` raise.

  - Layer 4 (transparency log): an append-only SHA-256-chained log of
    issued Charters. In-place tampering of any entry breaks `verify_chain`.

Crypto is real here — every test does a true Ed25519 sign / verify or a
true SHA-256 chain walk. Only the network layer is stubbed (so JWKS and
Charter fetches can be controlled per-test).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from charter import keys as keys_mod
from charter import transparency as tx
from charter.errors import CharterKeyMismatchError, CharterPinMismatchError
from charter.mcp_server import _fetch_and_verify
from charter.pins import fingerprint_of, get_pin
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
    verify_charter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign_with_keypair(
    *,
    principal_id: str = "alice@acme.com",
    agent_id: str = "agent_x",
    charter_id: str | None = None,
    keypair: tuple[Any, Any] | None = None,
) -> Charter:
    """Build + sign a Charter with a specified (or fresh) keypair."""
    private, public = keypair if keypair is not None else generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    cid = charter_id or f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}"
    charter = Charter(
        charter_id=cid,
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Crypto attack test."),
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


def _stub_charter_and_jwks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    charter: Charter,
    jwks_keypair_override: Any = None,
) -> None:
    """Route HTTP fetches so the Charter and JWKS bodies are controlled.

    If `jwks_keypair_override` is supplied, the JWKS publishes that
    other public key (under the SAME kid as the Charter declares) — the
    classic "host got compromised and rewrote both inline key and JWKS,
    but the JWKS still says the wrong key for this kid" scenario.
    """
    keys_mod.clear_cache()
    payload = charter.model_dump(mode="json")
    kid = charter.provenance.issuer_kid

    if jwks_keypair_override is not None:
        _, override_pub = jwks_keypair_override
        jwks_pubkey_str = public_key_to_string(override_pub)
    else:
        jwks_pubkey_str = charter.provenance.issuer_public_key

    jwks_body: dict[str, Any]
    if kid is not None:
        jwks_body = {"keys": [public_key_to_jwk(jwks_pubkey_str, kid=kid)]}
    else:
        jwks_body = {"keys": []}

    def fake_get(url: str, *, timeout: float = 10.0) -> httpx.Response:  # noqa: ARG001
        if "/.well-known/jwks.json" in url:
            return httpx.Response(200, json=jwks_body, request=httpx.Request("GET", url))
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr("charter.mcp_server.httpx.get", fake_get)
    monkeypatch.setattr("charter.keys.httpx.get", fake_get)


# ---------------------------------------------------------------------------
# (a) Signature replay across different charter_id
# ---------------------------------------------------------------------------


def test_signature_replay_across_different_charter_id_fails_verify(
    temp_data_dir: Path,  # noqa: ARG001
) -> None:
    """Attack: take a valid Charter A signed by key K, copy A's signature
    onto Charter B (different `charter_id`) signed nominally by the same
    key. Verification must reject B because `charter_id` is inside the
    canonical bytes — a copy-pasted signature does not cover B's payload.
    """
    original = _sign_with_keypair(charter_id="charter:alice:agent:2026-05-22:original")
    forged = _sign_with_keypair(
        charter_id="charter:alice:agent:2026-05-22:forged",
        keypair=None,  # fresh keypair, fresh signature
    )
    # Now overwrite forged's signature with the original's. Same key on
    # the inline pubkey (no — different keys). To stress only the
    # charter_id binding, build the forged Charter with the SAME inline
    # public key as the original.
    pk_str = original.provenance.issuer_public_key
    forged.provenance.issuer_public_key = pk_str
    forged.provenance.issuer_kid = original.provenance.issuer_kid
    forged.provenance.issuer_signature = original.provenance.issuer_signature

    # The signature was made over original's canonical bytes; forged has
    # a different charter_id, so the canonical bytes differ and verify
    # must fail.
    assert verify_charter(forged) is False


# ---------------------------------------------------------------------------
# (b) kid swap after signing
# ---------------------------------------------------------------------------


def test_kid_swap_after_signing_breaks_verify(temp_data_dir: Path) -> None:  # noqa: ARG001
    """Attack: an intermediary mutates `provenance.issuer_kid` post-sign
    to redirect verifiers to a JWKS entry the attacker controls. The
    kid is inside the signed payload, so the mutation breaks verify."""
    charter = _sign_with_keypair()
    original_kid = charter.provenance.issuer_kid
    assert original_kid is not None

    charter.provenance.issuer_kid = "0000000000000000"
    assert verify_charter(charter) is False

    # Restoring the original kid restores verification — proves the kid
    # itself was what broke it, not collateral state.
    charter.provenance.issuer_kid = original_kid
    assert verify_charter(charter) is True


# ---------------------------------------------------------------------------
# (c) transparency log entry hash tampering
# ---------------------------------------------------------------------------


def test_transparency_log_entry_hash_tampering_detected(temp_data_dir: Path) -> None:
    """Attack: an auditor running `verify_chain()` should detect any
    in-place mutation of the log. Here we change `binding.agent_id` on
    seq=1 without recomputing its entry_hash; the chain walker recomputes
    and reports a mismatch."""
    _sign_with_keypair(charter_id="charter:alice:agent_a:2026-05-22:1")
    _sign_with_keypair(charter_id="charter:alice:agent_b:2026-05-22:2")

    log_path = tx.log_file_path()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["binding"]["agent_id"] = "TAMPERED-BY-ATTACKER"
    lines[0] = json.dumps(first)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = tx.verify_chain()
    assert result.ok is False
    assert result.broken_at_seq == 1
    assert result.reason is not None and "entry_hash mismatch" in result.reason


def test_transparency_log_prev_hash_tampering_detected(temp_data_dir: Path) -> None:
    """Sibling case: tampering with `prev_hash` on a later entry must
    also be detected. Defends against an attacker who tries to splice
    in a fraudulent entry between two real ones."""
    _sign_with_keypair(charter_id="charter:alice:agent_a:2026-05-22:1")
    _sign_with_keypair(charter_id="charter:alice:agent_b:2026-05-22:2")
    _sign_with_keypair(charter_id="charter:alice:agent_c:2026-05-22:3")

    log_path = tx.log_file_path()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = "sha256:" + "f" * 64
    lines[1] = json.dumps(second)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = tx.verify_chain()
    assert result.ok is False
    assert result.broken_at_seq == 2
    assert "prev_hash mismatch" in (result.reason or "")


# ---------------------------------------------------------------------------
# (d) JWKS returns different key than inline pubkey
# ---------------------------------------------------------------------------


def test_jwks_returns_different_key_than_inline_raises_key_mismatch(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attack: the issuer's host signs a Charter with key K1 but the
    issuer's JWKS publishes a different key K2 under the same kid. ADR-007
    layer 2 catches this — the JWKS-published key does not match the
    Charter's inline `issuer_public_key`."""
    # Sign with K1.
    k1 = generate_keypair()
    charter = _sign_with_keypair(keypair=k1)

    # But publish K2 in JWKS under the same kid.
    k2 = generate_keypair()
    _stub_charter_and_jwks(monkeypatch, charter=charter, jwks_keypair_override=k2)

    with pytest.raises(CharterKeyMismatchError, match="disagrees with JWKS"):
        _fetch_and_verify("http://test.example.com/alice@acme.com/agent_x")


# ---------------------------------------------------------------------------
# (e) pin file bypass — attacker rewrites pins.json
# ---------------------------------------------------------------------------


def test_pin_file_bypass_attacker_writes_their_own_fingerprint(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attack: after a legitimate first fetch pins key K1, an attacker
    with file-system access rewrites `pins.json` to fingerprint K2
    (their own key). When a fresh Charter signed by K2 arrives, the pin
    check sees a 'match' against the planted fingerprint and accepts it.

    This documents the LIMIT of the pin layer: it protects against
    network attackers, not against attackers who already have local
    file-system write access. Mitigations: filesystem permissions,
    transparency log audit by an independent third party (which would
    catch the unexpected issuance from K2 even if pins agree).
    """
    # 1. Legitimate first fetch pins K1.
    k1 = generate_keypair()
    legit_charter = _sign_with_keypair(keypair=k1)
    _stub_charter_and_jwks(monkeypatch, charter=legit_charter)
    _fetch_and_verify("http://test.example.com/alice@acme.com/agent_x")

    pin = get_pin("alice@acme.com")
    assert pin is not None
    assert pin.fingerprint == fingerprint_of(legit_charter.provenance.issuer_public_key)

    # 2. Attacker rewrites pins.json to fingerprint K2 directly.
    k2 = generate_keypair()
    _, k2_pub = k2
    attacker_fp = fingerprint_of(public_key_to_string(k2_pub))
    pin_file = Path(temp_data_dir) / "pins.json"
    existing = json.loads(pin_file.read_text(encoding="utf-8"))
    existing["alice@acme.com"]["fingerprint"] = attacker_fp
    pin_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    # 3. Fresh Charter signed by K2 arrives. Pin check now matches the
    # planted fingerprint, so the fetch goes through — DOCUMENTED gap.
    forged_charter = _sign_with_keypair(keypair=k2)
    _stub_charter_and_jwks(monkeypatch, charter=forged_charter)
    result = _fetch_and_verify("http://test.example.com/alice@acme.com/agent_x")
    assert result.charter_id == forged_charter.charter_id


def test_pin_mismatch_on_unauthorized_key_swap(
    temp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
) -> None:
    """Counter-test: when pins.json is NOT tampered with, a key swap
    between two fetches raises `CharterPinMismatchError`. This is the
    layer working as designed against a pure network attacker."""
    k1 = generate_keypair()
    legit = _sign_with_keypair(keypair=k1)
    _stub_charter_and_jwks(monkeypatch, charter=legit)
    _fetch_and_verify("http://test.example.com/alice@acme.com/agent_x")

    # Attacker rotates the host to sign with K2 + publish K2 in JWKS.
    k2 = generate_keypair()
    forged = _sign_with_keypair(keypair=k2)
    _stub_charter_and_jwks(monkeypatch, charter=forged)

    with pytest.raises(CharterPinMismatchError, match="Pinned fingerprint"):
        _fetch_and_verify("http://test.example.com/alice@acme.com/agent_x")


# ---------------------------------------------------------------------------
# (bonus) signature wrong-key forgery
# ---------------------------------------------------------------------------


def test_signature_signed_by_different_key_than_inline_fails_verify(
    temp_data_dir: Path,  # noqa: ARG001
) -> None:
    """Attack: attacker signs the Charter with their own key K_attack but
    leaves the inline `issuer_public_key` as the legitimate K_legit. The
    inline-key verification at layer 1 catches this immediately."""
    k_legit_priv, k_legit_pub = generate_keypair()
    k_attack_priv, _ = generate_keypair()

    # Build a Charter with K_legit inline...
    charter = _sign_with_keypair(keypair=(k_legit_priv, k_legit_pub))
    # ...then re-sign with K_attack but keep the inline pubkey alone.
    payload_bytes = charter.model_dump_json().encode()  # arbitrary bytes for the sig
    raw_sig = k_attack_priv.sign(payload_bytes)
    charter.provenance.issuer_signature = f"ed25519:{base64.b64encode(raw_sig).decode('ascii')}"

    assert verify_charter(charter) is False
