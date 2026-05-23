"""Regenerate every JSON test vector under `conformance/vectors/`.

Run from the repo root:

    python conformance/generators/build_vectors.py

The generator uses the Charter Python API to PRODUCE the canonical outputs
that vectors then pin. Hand-editing `expected_output` of any vector defeats
the point — change the code, then rerun this script.

A handful of vectors use deterministic-looking placeholder keypairs (seeded
via the secret_key bytes literal) so the resulting `expected_output` is
stable across regenerations. The signing operation itself isn't otherwise
deterministic, but Ed25519 IS deterministic given the same private key +
the same canonical bytes, which is the property we exploit.

Output: one JSON file per vector, sorted lex by name within each subdir.
A `generation_metadata.json` is written at the top of `vectors/` recording
the charter version + git revision that produced the vectors.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Ensure repo root is on sys.path so `import charter` works regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from charter import __version__ as CHARTER_VERSION  # noqa: E402
from charter import privacy, signing, transparency  # noqa: E402
from charter.chain import _verify_chain_strict  # noqa: E402
from charter.constants import TYPE_TO_DECISION, aggregate_decision  # noqa: E402
from charter.pins import fingerprint_of  # noqa: E402
from charter.schema import (  # noqa: E402
    AgentOperator,
    AttenuationProof,
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
from charter.signing import (  # noqa: E402
    _canonical_bytes,
    kid_for_public_key,
    public_key_to_jwk,
    public_key_to_string,
    sign_charter,
    verify_charter,
)

VECTORS_DIR = _REPO_ROOT / "conformance" / "vectors"

# A fixed Ed25519 seed (32 bytes of "01") gives every regeneration the same
# private key. Ed25519 is deterministic under fixed key + fixed payload, so
# this is the trick that lets `expected_output` (containing signatures)
# stay stable across runs.
_FIXED_SEED = bytes([0x01]) * 32

# Deterministic timestamps for issued_at / valid_until / generated_at so
# canonical bytes are reproducible across machines / clocks.
_T0 = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
_T_VALID_UNTIL = _T0 + timedelta(days=30)
_T_EXPIRED_AT = _T0 - timedelta(days=10)
_T_EXPIRED_UNTIL = _T0 - timedelta(days=1)


def _fixed_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return a deterministic (private_key, public_key_str) for vector signing."""
    private = Ed25519PrivateKey.from_private_bytes(_FIXED_SEED)
    public = private.public_key()
    return private, public_key_to_string(public)


def _alt_keypair(seed_byte: int) -> tuple[Ed25519PrivateKey, str]:
    """A second deterministic keypair (different seed) for tamper / wrong-key tests."""
    private = Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)
    public = private.public_key()
    return private, public_key_to_string(public)


def _make_charter(
    public_key_str: str,
    *,
    charter_id: str = "charter:alice@acme.com:research_agent:2026-05-23",
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent",
    clauses: list[Clause] | None = None,
    issued_at: datetime = _T0,
    valid_until: datetime = _T_VALID_UNTIL,
    status: str = "active",
    revoked_at: datetime | None = None,
    summary_text: str = "A research agent operating within Alice's scope.",
) -> Charter:
    """Build a Charter with deterministic timestamps. Caller supplies clauses."""
    if clauses is None:
        clauses = [
            Clause(id="C-001", type="scope", text="Research scientific literature."),
            Clause(id="C-002", type="out_of_scope", text="Marketing copy."),
        ]
    return Charter(
        charter_id=charter_id,
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Researcher at Acme Corp."),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="acme_research_operator"),
        summary=Summary(plain_language=summary_text),
        clauses=clauses,
        lifecycle=Lifecycle(
            issued_at=issued_at,
            valid_until=valid_until,
            status=status,  # type: ignore[arg-type]
            revoked_at=revoked_at,
        ),
        provenance=Provenance(
            issuer_public_key=public_key_str,
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="alice's profile",
                    content_hash="sha256:" + "ab" * 32,
                )
            ],
            generated_at=issued_at,
        ),
    )


def _sign_without_log(charter: Charter, private: Ed25519PrivateKey) -> None:
    """Sign a Charter WITHOUT triggering the transparency log side effect.

    `signing.sign_charter` appends to `data/transparency.log` as part of the
    normal issuance flow, which is undesirable while generating vectors.
    We replicate the signing logic inline and skip the log append + the
    transparency_log_id assignment.
    """
    if charter.provenance.issuer_kid is None:
        charter.provenance.issuer_kid = kid_for_public_key(charter.provenance.issuer_public_key)
    payload = _canonical_bytes(charter)
    sig = private.sign(payload)
    charter.provenance.issuer_signature = f"ed25519:{base64.b64encode(sig).decode('ascii')}"


def _write_vector(subdir: str, filename: str, vector: dict[str, Any]) -> None:
    """Write one vector JSON file, with stable sorted-keys formatting."""
    target = VECTORS_DIR / subdir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(vector, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _charter_to_jsonable(charter: Charter) -> dict[str, Any]:
    """Pydantic JSON-mode dump, ready for embedding in a vector."""
    return charter.model_dump(mode="json")


# ---------------------------------------------------------------------------
# §1 + §2 — sign / canonical bytes
# ---------------------------------------------------------------------------


def gen_sign_vectors() -> None:
    """Vectors under vectors/sign/."""
    private, public_str = _fixed_keypair()

    # 1. Minimal Charter, canonical bytes SHA-256 pinned.
    base = _make_charter(public_str)
    payload = _canonical_bytes(base)
    _write_vector(
        "sign",
        "canonical_bytes_minimal.json",
        {
            "name": "Canonical bytes of a minimal signed Charter",
            "spec_section": "SPEC.md#1--canonical-bytes-for-signing-invariant-1-adr-003-adr-011",
            "input": {
                "operation": "canonical_bytes",
                "charter": _charter_to_jsonable(base),
            },
            "expected_output": {
                "canonical_bytes_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "canonical_bytes_length": len(payload),
            },
            "expected_error": None,
        },
    )

    # 2. Sign round-trip — deterministic Ed25519 given fixed seed + payload.
    signed = _make_charter(public_str)
    _sign_without_log(signed, private)
    _write_vector(
        "sign",
        "sign_roundtrip_deterministic.json",
        {
            "name": "Deterministic Ed25519 signing under fixed seed",
            "spec_section": "SPEC.md#2--ed25519-sign--verify-invariant-1-adr-002",
            "input": {
                "operation": "sign",
                "charter": _charter_to_jsonable(_make_charter(public_str)),
                "private_key_seed_hex": _FIXED_SEED.hex(),
            },
            "expected_output": {
                "issuer_signature": signed.provenance.issuer_signature,
                "issuer_kid": signed.provenance.issuer_kid,
            },
            "expected_error": None,
        },
    )

    # 3. Canonical bytes elide `private_fields: None` for backward compat.
    base_no_priv = _make_charter(public_str)
    payload_no_priv = _canonical_bytes(base_no_priv)
    assert b'"private_fields":null' not in payload_no_priv, (
        "regression: private_fields None should be elided"
    )
    _write_vector(
        "sign",
        "canonical_bytes_elides_private_fields_none.json",
        {
            "name": "private_fields=None is elided from canonical bytes (ADR-011 backward compat)",
            "spec_section": "SPEC.md#12-none-elision-backward-compatibility",
            "input": {
                "operation": "canonical_bytes_contains_substring",
                "charter": _charter_to_jsonable(base_no_priv),
                "substring": '"private_fields":null',
            },
            "expected_output": {"contains_substring": False},
            "expected_error": None,
        },
    )

    # 4. Canonical bytes exclude transparency_log_id. We compare two
    # Charters identical except one has transparency_log_id=null and the
    # other has it=42; both must hash identically because _canonical_bytes
    # clears the field before serialising.
    pre = _make_charter(public_str)
    pre_payload = _canonical_bytes(pre)
    post = _make_charter(public_str)
    post.provenance.transparency_log_id = 42
    post_payload = _canonical_bytes(post)
    assert pre_payload == post_payload, (
        "regression: transparency_log_id assignment leaked into canonical bytes"
    )
    _write_vector(
        "sign",
        "canonical_bytes_excludes_log_id.json",
        {
            "name": "transparency_log_id is excluded from canonical bytes",
            "spec_section": "SPEC.md#11-field-exclusions",
            "input": {
                "operation": "canonical_bytes_invariant_check",
                "charter_pre_assign": _charter_to_jsonable(pre),
                "charter_post_assign_log_id": 42,
            },
            "expected_output": {
                "pre_sha256": "sha256:" + hashlib.sha256(pre_payload).hexdigest(),
                "post_sha256": "sha256:" + hashlib.sha256(post_payload).hexdigest(),
                "must_be_equal": True,
            },
            "expected_error": None,
        },
    )

    # 5. Edited clause changes canonical bytes (sanity check of coverage).
    edited = _make_charter(public_str)
    edited.clauses[0].text = "Research scientific literature about quantum computing."
    edited_payload = _canonical_bytes(edited)
    base_payload = _canonical_bytes(_make_charter(public_str))
    assert edited_payload != base_payload
    _write_vector(
        "sign",
        "canonical_bytes_clause_edit_changes_hash.json",
        {
            "name": "Editing clause text changes canonical bytes hash",
            "spec_section": "SPEC.md#23-tamper-detection",
            "input": {
                "operation": "canonical_bytes_inequality",
                "charter_a": _charter_to_jsonable(_make_charter(public_str)),
                "charter_b": _charter_to_jsonable(edited),
            },
            "expected_output": {
                "sha256_a": "sha256:" + hashlib.sha256(base_payload).hexdigest(),
                "sha256_b": "sha256:" + hashlib.sha256(edited_payload).hexdigest(),
                "must_be_different": True,
            },
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# §2 — verify
# ---------------------------------------------------------------------------


def gen_verify_vectors() -> None:
    """Vectors under vectors/verify/."""
    private, public_str = _fixed_keypair()
    _, wrong_public_str = _alt_keypair(0x02)

    # 1. Valid signature.
    signed = _make_charter(public_str)
    _sign_without_log(signed, private)
    assert verify_charter(signed)
    _write_vector(
        "verify",
        "verify_valid_signature.json",
        {
            "name": "Verify a freshly signed Charter",
            "spec_section": "SPEC.md#22-verification-scope",
            "input": {
                "operation": "verify",
                "charter": _charter_to_jsonable(signed),
            },
            "expected_output": {"valid": True},
            "expected_error": None,
        },
    )

    # 2. Tampered clause text.
    tampered = _make_charter(public_str)
    _sign_without_log(tampered, private)
    tampered.clauses[0].text = "Research scientific literature AND market positioning."
    assert not verify_charter(tampered)
    _write_vector(
        "verify",
        "verify_tampered_clause.json",
        {
            "name": "Reject when a clause text has been edited post-signing",
            "spec_section": "SPEC.md#23-tamper-detection",
            "input": {
                "operation": "verify",
                "charter": _charter_to_jsonable(tampered),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 3. Tampered principal_id.
    tampered2 = _make_charter(public_str)
    _sign_without_log(tampered2, private)
    tampered2.binding.principal_id = "mallory@evil.com"
    tampered2.principal.id = "mallory@evil.com"
    assert not verify_charter(tampered2)
    _write_vector(
        "verify",
        "verify_tampered_principal.json",
        {
            "name": "Reject when principal_id has been swapped post-signing",
            "spec_section": "SPEC.md#23-tamper-detection",
            "input": {
                "operation": "verify",
                "charter": _charter_to_jsonable(tampered2),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 4. Wrong public key embedded (signature was made by `private` but the
    # Charter's stated public key is somebody else's).
    wrong_key = _make_charter(public_str)
    _sign_without_log(wrong_key, private)
    wrong_key.provenance.issuer_public_key = wrong_public_str
    assert not verify_charter(wrong_key)
    _write_vector(
        "verify",
        "verify_wrong_public_key.json",
        {
            "name": "Reject when embedded public key does not match signing key",
            "spec_section": "SPEC.md#22-verification-scope",
            "input": {
                "operation": "verify",
                "charter": _charter_to_jsonable(wrong_key),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 5. Non-Ed25519 algorithm prefix (`rsa:`) — must be rejected, NOT just
    # silently treated as invalid Ed25519.
    bad_alg = _make_charter(public_str)
    _sign_without_log(bad_alg, private)
    bad_alg.provenance.issuer_signature = "rsa:" + bad_alg.provenance.issuer_signature.removeprefix(
        "ed25519:"
    )
    assert not verify_charter(bad_alg)
    _write_vector(
        "verify",
        "verify_reject_non_ed25519_prefix.json",
        {
            "name": "Reject signature with non-ed25519 prefix (ADR-002)",
            "spec_section": "SPEC.md#21-algorithm",
            "input": {
                "operation": "verify",
                "charter": _charter_to_jsonable(bad_alg),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 6. Tampered lifecycle valid_until (date push-back).
    tampered_lc = _make_charter(public_str)
    _sign_without_log(tampered_lc, private)
    tampered_lc.lifecycle.valid_until = _T_VALID_UNTIL + timedelta(days=365)
    assert not verify_charter(tampered_lc)
    _write_vector(
        "verify",
        "verify_tampered_lifecycle.json",
        {
            "name": "Reject when valid_until has been extended post-signing",
            "spec_section": "SPEC.md#23-tamper-detection",
            "input": {
                "operation": "verify",
                "charter": _charter_to_jsonable(tampered_lc),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# §3 + §4 — aggregate verdict + TYPE_TO_DECISION
# ---------------------------------------------------------------------------


def gen_aggregate_vectors() -> None:
    """Vectors under vectors/aggregate/."""

    # TYPE_TO_DECISION coverage (one vector per row of the table).
    for clause_type, expected_decision in TYPE_TO_DECISION.items():
        _write_vector(
            "aggregate",
            f"type_to_decision_{clause_type}.json",
            {
                "name": f"TYPE_TO_DECISION['{clause_type}'] = {expected_decision!r}",
                "spec_section": "SPEC.md#3--type_to_decision-invariant-2-adr-004",
                "input": {
                    "operation": "type_to_decision",
                    "clause_type": clause_type,
                },
                "expected_output": {"local_decision": expected_decision},
                "expected_error": None,
            },
        )

    # Aggregate precedence.
    cases = [
        (["allow"], "allow", "single allow"),
        (["allow", "needs_approval"], "needs_approval", "needs_approval beats allow"),
        (
            ["allow", "needs_approval", "incompatible"],
            "incompatible",
            "incompatible beats both",
        ),
        (["needs_approval", "needs_approval"], "needs_approval", "all-same"),
        ([], "needs_approval", "zero-match closed-world fallback"),
        (["allow", "allow"], "allow", "all-allow"),
        (
            ["incompatible", "allow", "needs_approval"],
            "incompatible",
            "incompatible wins regardless of order",
        ),
    ]
    for i, (locals_, expected, description) in enumerate(cases, start=1):
        assert aggregate_decision(locals_) == expected  # type: ignore[arg-type]
        _write_vector(
            "aggregate",
            f"aggregate_{i:02d}_{description.replace(' ', '_')}.json",
            {
                "name": f"Aggregate ({description})",
                "spec_section": "SPEC.md#4--aggregate-verdict-invariant-3-adr-005",
                "input": {
                    "operation": "aggregate_decision",
                    "local_decisions": locals_,
                },
                "expected_output": {"aggregate_decision": expected},
                "expected_error": None,
            },
        )


# ---------------------------------------------------------------------------
# §6 — chain
# ---------------------------------------------------------------------------


def gen_chain_vectors() -> None:
    """Vectors under vectors/chain/. All strict-mode (no LLM required)."""
    _, parent_pub = _fixed_keypair()
    _, child_pub = _alt_keypair(0x03)

    # 1. Valid attenuation — child has narrower scope, preserves out_of_scope.
    parent = _make_charter(
        parent_pub,
        charter_id="charter:alice@acme.com:assistant:parent",
        agent_id="assistant",
        clauses=[
            Clause(id="P1", type="scope", text="Research."),
            Clause(id="P2", type="scope", text="Summarize."),
            Clause(id="P3", type="out_of_scope", text="Marketing copy."),
            Clause(id="P4", type="approval_required", text="External contact."),
        ],
    )
    child_valid = _make_charter(
        child_pub,
        charter_id="charter:assistant:subagent:child",
        principal_id="assistant",
        agent_id="subagent",
        clauses=[
            Clause(id="C1", type="scope", text="Research."),
            Clause(id="C2", type="out_of_scope", text="Marketing copy."),
            Clause(id="C3", type="approval_required", text="External contact."),
        ],
    )
    child_valid.parent_charter_url = "https://example.com/parent"
    child_valid.attenuation_proof = AttenuationProof(parent_charter_id=parent.charter_id)
    assert _verify_chain_strict(child_valid, parent)
    _write_vector(
        "chain",
        "chain_valid_attenuation.json",
        {
            "name": "Valid attenuation: child narrows scope, preserves restrictions",
            "spec_section": "SPEC.md#61-strict-string-based-mandatory",
            "input": {
                "operation": "verify_chain_strict",
                "child": _charter_to_jsonable(child_valid),
                "parent": _charter_to_jsonable(parent),
            },
            "expected_output": {"valid": True},
            "expected_error": None,
        },
    )

    # 2. Child relaxes out_of_scope — chain must reject.
    child_relaxes = _make_charter(
        child_pub,
        charter_id="charter:assistant:subagent:relaxes",
        principal_id="assistant",
        agent_id="subagent",
        clauses=[
            Clause(id="C1", type="scope", text="Research."),
            # Note: parent's "Marketing copy." not preserved.
        ],
    )
    child_relaxes.attenuation_proof = AttenuationProof(parent_charter_id=parent.charter_id)
    assert not _verify_chain_strict(child_relaxes, parent)
    _write_vector(
        "chain",
        "chain_rejects_relaxed_out_of_scope.json",
        {
            "name": "Reject when child drops a parent out_of_scope clause",
            "spec_section": "SPEC.md#61-strict-string-based-mandatory",
            "input": {
                "operation": "verify_chain_strict",
                "child": _charter_to_jsonable(child_relaxes),
                "parent": _charter_to_jsonable(parent),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 3. Child widens scope (adds scope clause parent did not have).
    child_widens = _make_charter(
        child_pub,
        charter_id="charter:assistant:subagent:widens",
        principal_id="assistant",
        agent_id="subagent",
        clauses=[
            Clause(id="C1", type="scope", text="Research."),
            Clause(id="C2", type="scope", text="Trading."),  # parent has no "Trading."
            Clause(id="C3", type="out_of_scope", text="Marketing copy."),
            Clause(id="C4", type="approval_required", text="External contact."),
        ],
    )
    child_widens.attenuation_proof = AttenuationProof(parent_charter_id=parent.charter_id)
    assert not _verify_chain_strict(child_widens, parent)
    _write_vector(
        "chain",
        "chain_rejects_widened_scope.json",
        {
            "name": "Reject when child adds a scope clause parent didn't authorize",
            "spec_section": "SPEC.md#61-strict-string-based-mandatory",
            "input": {
                "operation": "verify_chain_strict",
                "child": _charter_to_jsonable(child_widens),
                "parent": _charter_to_jsonable(parent),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 4. attenuation_proof.parent_charter_id mismatches.
    child_wrong_proof = _make_charter(
        child_pub,
        charter_id="charter:assistant:subagent:wrong_proof",
        principal_id="assistant",
        agent_id="subagent",
        clauses=[
            Clause(id="C1", type="scope", text="Research."),
            Clause(id="C2", type="out_of_scope", text="Marketing copy."),
            Clause(id="C3", type="approval_required", text="External contact."),
        ],
    )
    child_wrong_proof.attenuation_proof = AttenuationProof(
        parent_charter_id="charter:somebody-else:fake_parent"
    )
    assert not _verify_chain_strict(child_wrong_proof, parent)
    _write_vector(
        "chain",
        "chain_rejects_wrong_parent_id.json",
        {
            "name": "Reject when attenuation_proof.parent_charter_id mismatches",
            "spec_section": "SPEC.md#61-strict-string-based-mandatory",
            "input": {
                "operation": "verify_chain_strict",
                "child": _charter_to_jsonable(child_wrong_proof),
                "parent": _charter_to_jsonable(parent),
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 5. Child uses superstring containment for out_of_scope — allowed.
    child_superstring = _make_charter(
        child_pub,
        charter_id="charter:assistant:subagent:superstring",
        principal_id="assistant",
        agent_id="subagent",
        clauses=[
            Clause(id="C1", type="scope", text="Research."),
            Clause(
                id="C2",
                type="out_of_scope",
                text="Marketing copy. OR cold-email campaigns.",
            ),
            Clause(id="C3", type="approval_required", text="External contact."),
        ],
    )
    child_superstring.attenuation_proof = AttenuationProof(parent_charter_id=parent.charter_id)
    assert _verify_chain_strict(child_superstring, parent)
    _write_vector(
        "chain",
        "chain_superstring_out_of_scope.json",
        {
            "name": "Accept superstring out_of_scope (child tightens by adding more)",
            "spec_section": "SPEC.md#61-strict-string-based-mandatory",
            "input": {
                "operation": "verify_chain_strict",
                "child": _charter_to_jsonable(child_superstring),
                "parent": _charter_to_jsonable(parent),
            },
            "expected_output": {"valid": True},
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# §5 — lifecycle
# ---------------------------------------------------------------------------


def gen_lifecycle_vectors() -> None:
    """Vectors under vectors/lifecycle/."""
    _, public_str = _fixed_keypair()

    # 1. active.
    active = _make_charter(public_str)
    _write_vector(
        "lifecycle",
        "lifecycle_active.json",
        {
            "name": "active status, valid_until in future",
            "spec_section": "SPEC.md#5--lifecycle-state-machine-invariant",
            "input": {
                "operation": "lifecycle_status",
                "charter": _charter_to_jsonable(active),
                "now_iso": _T0.isoformat(),
            },
            "expected_output": {
                "status": "active",
                "is_expired": False,
                "is_revoked": False,
                "is_superseded": False,
                "policy_classification": "usable",
            },
            "expected_error": None,
        },
    )

    # 2. expired (status still "active" on paper but valid_until passed).
    expired = _make_charter(
        public_str,
        issued_at=_T_EXPIRED_AT,
        valid_until=_T_EXPIRED_UNTIL,
    )
    _write_vector(
        "lifecycle",
        "lifecycle_expired.json",
        {
            "name": "expired by valid_until being in the past",
            "spec_section": "SPEC.md#5--lifecycle-state-machine-invariant",
            "input": {
                "operation": "lifecycle_status",
                "charter": _charter_to_jsonable(expired),
                "now_iso": _T0.isoformat(),
            },
            "expected_output": {
                "status": "active",
                "is_expired": True,
                "is_revoked": False,
                "is_superseded": False,
                "policy_classification": "needs_approval_or_incompatible",
            },
            "expected_error": None,
        },
    )

    # 3. revoked.
    revoked = _make_charter(
        public_str,
        status="revoked",
        revoked_at=_T0 - timedelta(hours=1),
    )
    _write_vector(
        "lifecycle",
        "lifecycle_revoked.json",
        {
            "name": "revoked status, revoked_at set",
            "spec_section": "SPEC.md#5--lifecycle-state-machine-invariant",
            "input": {
                "operation": "lifecycle_status",
                "charter": _charter_to_jsonable(revoked),
                "now_iso": _T0.isoformat(),
            },
            "expected_output": {
                "status": "revoked",
                "is_expired": False,
                "is_revoked": True,
                "is_superseded": False,
                "policy_classification": "incompatible",
            },
            "expected_error": None,
        },
    )

    # 4. superseded (replaced_by set).
    superseded = _make_charter(public_str, status="superseded")
    superseded.lifecycle.replaced_by = "charter:alice@acme.com:research_agent:2026-06-01"
    _write_vector(
        "lifecycle",
        "lifecycle_superseded.json",
        {
            "name": "superseded status, replaced_by populated",
            "spec_section": "SPEC.md#5--lifecycle-state-machine-invariant",
            "input": {
                "operation": "lifecycle_status",
                "charter": _charter_to_jsonable(superseded),
                "now_iso": _T0.isoformat(),
            },
            "expected_output": {
                "status": "superseded",
                "is_expired": False,
                "is_revoked": False,
                "is_superseded": True,
                "policy_classification": "redirect_to_successor",
            },
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# §7 + §9 — JWKS + pins
# ---------------------------------------------------------------------------


def gen_jwks_vectors() -> None:
    """Vectors under vectors/jwks/."""
    _, public_str = _fixed_keypair()

    # 1. JWK derivation.
    jwk = public_key_to_jwk(public_str)
    _write_vector(
        "jwks",
        "jwk_derivation_minimal.json",
        {
            "name": "Derive RFC 7517 JWK from ed25519:<base64> public key string",
            "spec_section": "SPEC.md#71-jwk-shape",
            "input": {
                "operation": "public_key_to_jwk",
                "public_key": public_str,
            },
            "expected_output": jwk,
            "expected_error": None,
        },
    )

    # 2. kid derivation stability.
    kid = kid_for_public_key(public_str)
    _write_vector(
        "jwks",
        "kid_derivation_stable.json",
        {
            "name": "kid is first 16 hex chars of sha256(raw_public_key)",
            "spec_section": "SPEC.md#72-kid-derivation",
            "input": {
                "operation": "kid_for_public_key",
                "public_key": public_str,
            },
            "expected_output": {"kid": kid, "kid_length_chars": 16},
            "expected_error": None,
        },
    )

    # 3. Pin fingerprint.
    fp = fingerprint_of(public_str)
    _write_vector(
        "jwks",
        "pin_fingerprint.json",
        {
            "name": "Pin fingerprint is sha256:<full-hex> of raw public key bytes",
            "spec_section": "SPEC.md#91-fingerprint-format",
            "input": {
                "operation": "pin_fingerprint",
                "public_key": public_str,
            },
            "expected_output": {"fingerprint": fp, "hex_length_chars": 64},
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# §8 — transparency log
# ---------------------------------------------------------------------------


def gen_transparency_vectors() -> None:
    """Vectors under vectors/transparency/."""
    # Build a small in-memory log with deterministic timestamps + deterministic
    # entry fields so the SHA-256 chain is stable.
    private, public_str = _fixed_keypair()

    def _entry(seq: int, charter_id: str, prev_hash: str) -> dict[str, Any]:
        # Sign a charter to get a deterministic signature.
        c = _make_charter(public_str, charter_id=charter_id)
        _sign_without_log(c, private)
        payload: dict[str, Any] = {
            "seq": seq,
            "charter_id": charter_id,
            "binding": {
                "principal_id": c.binding.principal_id,
                "agent_id": c.binding.agent_id,
            },
            "issuer_kid": c.provenance.issuer_kid,
            "issuer_signature": c.provenance.issuer_signature,
            "appended_at": _T0.isoformat(),
            "prev_hash": prev_hash,
        }
        entry_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        payload["entry_hash"] = entry_hash
        return payload

    genesis = transparency.GENESIS_PREV_HASH
    e1 = _entry(1, "charter:alice@acme.com:agent1:t1", genesis)
    e2 = _entry(2, "charter:alice@acme.com:agent2:t2", e1["entry_hash"])
    e3 = _entry(3, "charter:alice@acme.com:agent3:t3", e2["entry_hash"])

    # 1. Valid 3-entry chain.
    _write_vector(
        "transparency",
        "log_valid_chain.json",
        {
            "name": "Three-entry transparency log with valid SHA-256 chain",
            "spec_section": "SPEC.md#83-verification",
            "input": {
                "operation": "verify_log_chain",
                "entries": [e1, e2, e3],
            },
            "expected_output": {
                "ok": True,
                "entries": 3,
                "head_hash": e3["entry_hash"],
            },
            "expected_error": None,
        },
    )

    # 2. Empty log is OK (genesis state).
    _write_vector(
        "transparency",
        "log_empty.json",
        {
            "name": "Empty log verifies trivially with head=genesis",
            "spec_section": "SPEC.md#83-verification",
            "input": {
                "operation": "verify_log_chain",
                "entries": [],
            },
            "expected_output": {
                "ok": True,
                "entries": 0,
                "head_hash": transparency.GENESIS_PREV_HASH,
            },
            "expected_error": None,
        },
    )

    # 3. Tampered prev_hash (break the chain).
    tampered_entries = [dict(e1), dict(e2), dict(e3)]
    tampered_entries[2]["prev_hash"] = "sha256:" + "0" * 64  # wrong link
    _write_vector(
        "transparency",
        "log_tampered_prev_hash.json",
        {
            "name": "Reject log when seq=3's prev_hash no longer matches seq=2's entry_hash",
            "spec_section": "SPEC.md#83-verification",
            "input": {
                "operation": "verify_log_chain",
                "entries": tampered_entries,
            },
            "expected_output": {
                "ok": False,
                "broken_at_seq": 3,
            },
            "expected_error": None,
        },
    )

    # 4. Tampered entry body (entry_hash recomputation fails).
    tampered_body = [dict(e1), dict(e2), dict(e3)]
    tampered_body[1] = dict(e2)
    tampered_body[1]["charter_id"] = "charter:mallory@evil.com:tampered:t2"
    # Keep the old entry_hash so the recomputation will differ.
    _write_vector(
        "transparency",
        "log_tampered_entry_body.json",
        {
            "name": "Reject log when an entry's body changed but entry_hash wasn't recomputed",
            "spec_section": "SPEC.md#83-verification",
            "input": {
                "operation": "verify_log_chain",
                "entries": tampered_body,
            },
            "expected_output": {
                "ok": False,
                "broken_at_seq": 2,
            },
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# §10 — privacy / SD-JWT path 1
# ---------------------------------------------------------------------------


def gen_privacy_vectors() -> None:
    """Vectors under vectors/privacy/. Uses fixed salt for determinism."""
    fixed_salt = bytes.fromhex("0123456789abcdef0123456789abcdef")
    clause_text = "Handle customer Alice Wonderland with care."
    span = (16, 31)  # "Alice Wonderland"
    redacted_text, fields, disclosures = privacy.redact_clause(
        clause_text, [span], salt=fixed_salt
    )

    # 1. Redaction roundtrip — deterministic under fixed salt.
    _write_vector(
        "privacy",
        "redact_clause_roundtrip.json",
        {
            "name": "redact_clause produces matching placeholder + disclosure under fixed salt",
            "spec_section": "SPEC.md#101-per-span-redaction",
            "input": {
                "operation": "redact_clause",
                "clause_text": clause_text,
                "private_spans": [list(span)],
                "salt_hex": fixed_salt.hex(),
            },
            "expected_output": {
                "redacted_text": redacted_text,
                "private_fields": [f.model_dump(mode="json") for f in fields],
                "disclosures": [d.model_dump(mode="json") for d in disclosures],
            },
            "expected_error": None,
        },
    )

    # 2. verify_disclosure happy path.
    d = disclosures[0]
    assert privacy.verify_disclosure(d, d.disclosure_hash)
    _write_vector(
        "privacy",
        "verify_disclosure_valid.json",
        {
            "name": "verify_disclosure returns True for matching salt+value+hash",
            "spec_section": "SPEC.md#104-disclosure-verification",
            "input": {
                "operation": "verify_disclosure",
                "disclosure": d.model_dump(mode="json"),
                "claimed_hash": d.disclosure_hash,
            },
            "expected_output": {"valid": True},
            "expected_error": None,
        },
    )

    # 3. verify_disclosure rejects tampered span_value.
    tampered = privacy.Disclosure(
        disclosure_id=d.disclosure_id,
        span_value="Mallory Adversarial",  # changed
        salt_hex=d.salt_hex,
        disclosure_hash=d.disclosure_hash,  # stale
    )
    assert not privacy.verify_disclosure(tampered, d.disclosure_hash)
    _write_vector(
        "privacy",
        "verify_disclosure_tampered_value.json",
        {
            "name": "verify_disclosure returns False when span_value was edited",
            "spec_section": "SPEC.md#104-disclosure-verification",
            "input": {
                "operation": "verify_disclosure",
                "disclosure": tampered.model_dump(mode="json"),
                "claimed_hash": d.disclosure_hash,
            },
            "expected_output": {"valid": False},
            "expected_error": None,
        },
    )

    # 4. Plaintext is NOT in canonical bytes (the headline invariant of §10.3).
    _, public_str = _fixed_keypair()
    charter_with_redaction = _make_charter(public_str)
    # Splice the redacted clause + private_fields into clauses[0].
    charter_with_redaction.clauses[0].text = redacted_text
    charter_with_redaction.clauses[0].private_fields = fields
    payload = _canonical_bytes(charter_with_redaction)
    leaked = b"Alice Wonderland" in payload
    assert not leaked
    _write_vector(
        "privacy",
        "plaintext_never_in_canonical_bytes.json",
        {
            "name": "Plaintext of a redacted span never appears in canonical bytes",
            "spec_section": "SPEC.md#103-canonical-bytes-participation",
            "input": {
                "operation": "canonical_bytes_excludes_plaintext",
                "charter": _charter_to_jsonable(charter_with_redaction),
                "plaintext": "Alice Wonderland",
            },
            "expected_output": {"plaintext_in_canonical_bytes": False},
            "expected_error": None,
        },
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except (OSError, FileNotFoundError):
        return None


def _write_generation_metadata(vector_count: int) -> None:
    meta = {
        "spec_version": "0.1",
        "charter_version": CHARTER_VERSION,
        "git_revision": _git_revision(),
        "regenerated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_vectors": vector_count,
        "notes": (
            "All vectors generated by `python conformance/generators/build_vectors.py`. "
            "Do NOT hand-edit `expected_output`; regenerate from charter Python API."
        ),
    }
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    (VECTORS_DIR / "generation_metadata.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    # Make transparency.append a true no-op even if a generator subroutine
    # accidentally goes through sign_charter — protect the production log.
    os.environ["CHARTER_TRANSPARENCY_LOG"] = str(VECTORS_DIR / "_scratch.log")

    # Clear vector subdirs (NOT the runners or top-level metadata yet).
    for sub in ["sign", "verify", "aggregate", "chain", "lifecycle", "jwks", "transparency", "privacy"]:
        target = VECTORS_DIR / sub
        if target.exists():
            shutil.rmtree(target)

    gen_sign_vectors()
    gen_verify_vectors()
    gen_aggregate_vectors()
    gen_chain_vectors()
    gen_lifecycle_vectors()
    gen_jwks_vectors()
    gen_transparency_vectors()
    gen_privacy_vectors()

    # Clean up the scratch log if it was created.
    scratch = VECTORS_DIR / "_scratch.log"
    if scratch.exists():
        scratch.unlink()

    # Count generated vectors.
    total = sum(1 for _ in VECTORS_DIR.rglob("*.json") if _.name != "generation_metadata.json")
    _write_generation_metadata(total)

    print(f"Generated {total} vectors under {VECTORS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
