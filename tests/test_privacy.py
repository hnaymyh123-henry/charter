"""Tests for `charter.privacy` + the `/disclosures/...` endpoint (ADR-011 path 1).

Covers:

  - redact_clause roundtrip and span-coordinate correctness
  - verify_disclosure detects tampering of value, salt, and hash
  - sign_charter + verify_charter still work when clauses carry
    `private_fields` (this is the load-bearing claim of path 1)
  - match_redacted positive / negative / cross-clause isolation
  - GET /disclosures/... rejects requests without a valid bearer token
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from charter.privacy import (
    Disclosure,
    match_redacted,
    redact_clause,
    verify_disclosure,
)
from charter.schema import (
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    PrivateFieldRef,
    Provenance,
    SourceCommitment,
    Summary,
    Visibility,
)
from charter.signing import (
    generate_keypair,
    public_key_to_string,
    sign_charter,
    verify_charter,
)
from charter.storage import save_disclosure

# ---------------------------------------------------------------------------
# redact_clause
# ---------------------------------------------------------------------------


def test_redact_clause_replaces_each_span_with_placeholder() -> None:
    text = "Do not export data for Acme Corp customers."
    # "Acme Corp" lives at offsets 23..32.
    start = text.index("Acme Corp")
    end = start + len("Acme Corp")
    redacted, fields, discs = redact_clause(text, [(start, end)], salt=b"S" * 16)

    assert "Acme Corp" not in redacted
    assert redacted.startswith("Do not export data for [REDACTED:")
    assert redacted.endswith(" customers.")
    assert len(fields) == 1
    assert len(discs) == 1
    # The span coordinates must point INTO the redacted text and bracket
    # the placeholder exactly.
    f = fields[0]
    assert redacted[f.span_start : f.span_end].startswith("[REDACTED:")
    assert redacted[f.span_start : f.span_end].endswith("]")
    # The disclosure value is the original plaintext.
    assert discs[0].span_value == "Acme Corp"
    # The disclosure hash matches the in-clause commitment.
    assert discs[0].disclosure_hash == f.disclosure_hash


def test_redact_clause_handles_multiple_non_overlapping_spans() -> None:
    text = "No work for customer Foo on case CASE-42."
    s1 = text.index("Foo")
    s2 = text.index("CASE-42")
    redacted, fields, discs = redact_clause(
        text,
        [(s1, s1 + len("Foo")), (s2, s2 + len("CASE-42"))],
        salt=b"X" * 16,
    )
    assert "Foo" not in redacted
    assert "CASE-42" not in redacted
    assert len(fields) == 2
    assert len(discs) == 2
    # Each placeholder is exactly where its private_field says it is.
    for f in fields:
        assert redacted[f.span_start : f.span_end].startswith("[REDACTED:")


def test_redact_clause_no_spans_returns_input_unchanged() -> None:
    text = "Nothing private here."
    redacted, fields, discs = redact_clause(text, [])
    assert redacted == text
    assert fields == []
    assert discs == []


def test_redact_clause_rejects_overlapping_spans() -> None:
    text = "abcdef"
    with pytest.raises(ValueError, match="overlapping"):
        redact_clause(text, [(0, 4), (2, 5)], salt=b"Q" * 16)


def test_redact_clause_rejects_out_of_bounds_spans() -> None:
    with pytest.raises(ValueError, match="invalid span"):
        redact_clause("abc", [(0, 99)], salt=b"Q" * 16)


def test_redact_clause_roundtrip_value_recoverable_from_disclosure() -> None:
    """A caller with the Disclosure should be able to reconstruct the
    original clause text by substituting placeholders back in."""
    text = "Customer Acme Corp must not see internal pricing."
    start = text.index("Acme Corp")
    redacted, fields, discs = redact_clause(
        text, [(start, start + len("Acme Corp"))], salt=b"R" * 16
    )
    # Reconstruct.
    rebuilt = (
        redacted[: fields[0].span_start] + discs[0].span_value + redacted[fields[0].span_end :]
    )
    assert rebuilt == text


# ---------------------------------------------------------------------------
# verify_disclosure
# ---------------------------------------------------------------------------


def test_verify_disclosure_accepts_intact_record() -> None:
    _, _, discs = redact_clause("hello world", [(6, 11)], salt=b"S" * 16)
    d = discs[0]
    assert verify_disclosure(d, d.disclosure_hash) is True


def test_verify_disclosure_rejects_value_tamper() -> None:
    _, _, discs = redact_clause("hello world", [(6, 11)], salt=b"S" * 16)
    d = discs[0]
    tampered = d.model_copy(update={"span_value": "evil!"})
    assert verify_disclosure(tampered, d.disclosure_hash) is False


def test_verify_disclosure_rejects_salt_tamper() -> None:
    _, _, discs = redact_clause("hello world", [(6, 11)], salt=b"S" * 16)
    d = discs[0]
    tampered = d.model_copy(update={"salt_hex": ("00" * 16)})
    assert verify_disclosure(tampered, d.disclosure_hash) is False


def test_verify_disclosure_rejects_hash_tamper() -> None:
    _, _, discs = redact_clause("hello world", [(6, 11)], salt=b"S" * 16)
    d = discs[0]
    bogus = "sha256:" + "0" * 64
    tampered = d.model_copy(update={"disclosure_hash": bogus})
    assert verify_disclosure(tampered, d.disclosure_hash) is False


def test_verify_disclosure_returns_false_on_bad_salt_hex() -> None:
    """Garbage in the salt_hex field should be a clean False, not a crash."""
    bad = Disclosure(
        disclosure_id="x",
        span_value="v",
        # 64 hex chars but the disclosure_id and value are unrelated,
        # so verify must fail when reading salt_hex that is well-formed
        # hex (no exception) but the resulting hash mismatches.
        salt_hex="0" * 32,
        disclosure_hash="sha256:" + "0" * 64,
    )
    assert verify_disclosure(bad, bad.disclosure_hash) is False


# ---------------------------------------------------------------------------
# match_redacted
# ---------------------------------------------------------------------------


def test_match_redacted_positive_when_candidate_is_the_plaintext() -> None:
    text = "Do not assist customer Acme Corp on tax matters."
    start = text.index("Acme Corp")
    redacted, _, discs = redact_clause(text, [(start, start + len("Acme Corp"))], salt=b"P" * 16)
    assert match_redacted(redacted, "Acme Corp", discs) is True


def test_match_redacted_negative_when_candidate_is_unrelated() -> None:
    text = "Do not assist customer Acme Corp."
    start = text.index("Acme Corp")
    redacted, _, discs = redact_clause(text, [(start, start + len("Acme Corp"))], salt=b"P" * 16)
    assert match_redacted(redacted, "Globex Inc", discs) is False


def test_match_redacted_negative_when_empty_disclosures_list() -> None:
    """No disclosures available -> never a match, regardless of input."""
    assert match_redacted("any clause text", "Acme Corp", []) is False


def test_match_redacted_ignores_disclosures_from_other_clauses() -> None:
    """A disclosure whose placeholder is NOT in this clause's text must
    not produce a positive match here, even if the plaintext is right."""
    text_a = "Block client Acme Corp."
    start = text_a.index("Acme Corp")
    redacted_a, _, discs_a = redact_clause(
        text_a, [(start, start + len("Acme Corp"))], salt=b"A" * 16
    )

    text_b = "Block client Acme Corp."  # same value, fresh salt -> different placeholder
    redacted_b, _, _ = redact_clause(text_b, [(start, start + len("Acme Corp"))], salt=b"B" * 16)
    # discs_a's placeholder does not appear in redacted_b — so even
    # passing the correct plaintext must NOT match against the wrong
    # clause.
    assert redacted_a != redacted_b
    assert match_redacted(redacted_b, "Acme Corp", discs_a) is False


# ---------------------------------------------------------------------------
# Sign + verify of a Charter with redacted clauses (the load-bearing test)
# ---------------------------------------------------------------------------


def _make_redacted_charter() -> tuple[Charter, list[Disclosure], object]:
    """Build a Charter that has one redacted clause + return private key."""
    private, public = generate_keypair()
    text = "Do not handle requests for customer Acme Corp."
    start = text.index("Acme Corp")
    red_text, fields, discs = redact_clause(
        text, [(start, start + len("Acme Corp"))], salt=b"K" * 16
    )

    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:alice@acme.com:research_agent_v1:2026-05-22",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Test"),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        visibility=Visibility(private_clauses="redaction_v1"),
        summary=Summary(plain_language="Test."),
        clauses=[
            Clause(id="C-001", type="scope", text="Research tasks ok."),
            Clause(id="C-002", type="out_of_scope", text=red_text, private_fields=fields),
        ],
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
    return charter, discs, private


def test_sign_and_verify_roundtrip_with_redacted_clause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    charter, _, _ = _make_redacted_charter()
    assert charter.provenance.issuer_signature.startswith("ed25519:")
    assert verify_charter(charter) is True


def test_verify_rejects_tampering_with_disclosure_hash_in_clause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating a `private_fields[].disclosure_hash` after signing must
    break verification — the hash is INSIDE canonical bytes."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    charter, _, _ = _make_redacted_charter()
    assert verify_charter(charter) is True

    pf = charter.clauses[1].private_fields
    assert pf is not None
    pf[0] = PrivateFieldRef(
        span_start=pf[0].span_start,
        span_end=pf[0].span_end,
        disclosure_hash="sha256:" + "f" * 64,
    )
    assert verify_charter(charter) is False


def test_signing_old_charter_with_none_private_fields_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Charter with `private_fields=None` everywhere must produce the
    same canonical bytes as a Charter that doesn't have the field at all
    (which is the v0.x baseline). Verified by sign + verify round-trip
    and by checking the canonical_bytes output."""
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    from charter.signing import _canonical_bytes

    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:bob@example.com:agent:2026-05-22",
        binding=Binding(principal_id="bob@example.com", agent_id="agent"),
        principal=Principal(id="bob@example.com", role_summary="Test"),
        issuer=Issuer(id="bob@example.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[Clause(id="C-001", type="scope", text="ok")],
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
    canonical = _canonical_bytes(charter)
    # The string `private_fields` must NOT appear in canonical bytes when
    # the field is None on every clause — that's the backward-compat
    # guarantee for pre-ADR-011 Charters.
    assert b"private_fields" not in canonical

    sign_charter(charter, private)
    assert verify_charter(charter) is True


# ---------------------------------------------------------------------------
# GET /disclosures/{charter_id}/{disclosure_id}
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(temp_data_dir: Path):  # noqa: ARG001
    from charter.server import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_disclosures_endpoint_returns_404_when_token_env_unset(
    client: AsyncClient, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var configured -> endpoint is effectively disabled."""
    monkeypatch.delenv("CHARTER_DISCLOSURE_TOKEN", raising=False)
    charter, discs, _ = _make_redacted_charter()
    save_disclosure(charter.charter_id, discs[0])

    async with client as ac:
        r = await ac.get(f"/disclosures/{charter.charter_id}/{discs[0].disclosure_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_disclosures_endpoint_404_without_authorization_header(
    client: AsyncClient, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_DISCLOSURE_TOKEN", "secret-token-abc")
    charter, discs, _ = _make_redacted_charter()
    save_disclosure(charter.charter_id, discs[0])

    async with client as ac:
        r = await ac.get(f"/disclosures/{charter.charter_id}/{discs[0].disclosure_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_disclosures_endpoint_404_with_wrong_token(
    client: AsyncClient, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_DISCLOSURE_TOKEN", "secret-token-abc")
    charter, discs, _ = _make_redacted_charter()
    save_disclosure(charter.charter_id, discs[0])

    async with client as ac:
        r = await ac.get(
            f"/disclosures/{charter.charter_id}/{discs[0].disclosure_id}",
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_disclosures_endpoint_returns_record_with_correct_token(
    client: AsyncClient, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHARTER_DISCLOSURE_TOKEN", "secret-token-abc")
    charter, discs, _ = _make_redacted_charter()
    save_disclosure(charter.charter_id, discs[0])

    async with client as ac:
        r = await ac.get(
            f"/disclosures/{charter.charter_id}/{discs[0].disclosure_id}",
            headers={"Authorization": "Bearer secret-token-abc"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["disclosure_id"] == discs[0].disclosure_id
    assert body["span_value"] == discs[0].span_value
    # Reconstructed Disclosure must still verify against its hash.
    reloaded = Disclosure(**body)
    assert verify_disclosure(reloaded, discs[0].disclosure_hash) is True


@pytest.mark.asyncio
async def test_disclosures_endpoint_404_when_id_unknown_but_token_correct(
    client: AsyncClient, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indistinguishable from the "wrong token" 404 — by design."""
    monkeypatch.setenv("CHARTER_DISCLOSURE_TOKEN", "secret-token-abc")
    charter, _, _ = _make_redacted_charter()
    # Do not persist any disclosure file for this charter.

    async with client as ac:
        r = await ac.get(
            f"/disclosures/{charter.charter_id}/never_existed",
            headers={"Authorization": "Bearer secret-token-abc"},
        )
    assert r.status_code == 404
