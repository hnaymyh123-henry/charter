"""Tests for `charter.adapters.ap2`.

These tests inject stub `fetch_charter_fn` and `hits_grader` callables
so no network / LLM traffic is required. The grader stubs deliberately
mirror the shape a real LLM grader would emit so the adapter's
aggregation path runs end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from charter.adapters.ap2 import embed_charter_in_mandate, verify
from charter.errors import CharterNotFoundError, CharterSignatureError
from charter.schema import (
    AgentOperator,
    AP2VerifyResult,
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _signed_charter() -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    c = Charter(
        charter_id="charter:alice@acme.com:pay_agent_v1:test",
        binding=Binding(principal_id="alice@acme.com", agent_id="pay_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Test."),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test charter."),
        clauses=[
            Clause(id="C-001", type="scope", text="Low-value transfers."),
            Clause(id="C-002", type="approval_required", text="Large transfers."),
            Clause(id="C-003", type="out_of_scope", text="Wire fraud."),
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
def charter() -> Charter:
    return _signed_charter()


@pytest.fixture
def fetch_charter_ok(charter: Charter):
    def _fetch(_url: str) -> Charter:
        return charter

    return _fetch


def _grader(hits: list[dict[str, Any]]):
    def grade(_charter: Charter, _task: str) -> list[dict[str, Any]]:
        return list(hits)

    return grade


def _base_mandate(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid AP2 mandate dict."""
    mandate: dict[str, Any] = {
        "payer": "alice@acme.com",
        "payee": "merchant@example.com",
        "amount": {"value": 200.0, "currency": "USD"},
        "task": "Pay $200 to merchant for invoice #123.",
        "signature": "ap2-sig-mock",
        "extensions": {"charter_url": "https://test/alice@acme.com/pay_agent_v1"},
    }
    mandate.update(overrides)
    return mandate


# ---------------------------------------------------------------------------
# embed_charter_in_mandate
# ---------------------------------------------------------------------------


def test_embed_roundtrip_preserves_other_fields():
    """Embedding charter_url leaves payer/payee/amount/etc. untouched."""
    original = {
        "payer": "alice",
        "payee": "bob",
        "amount": {"value": 50.0, "currency": "USD"},
        "signature": "sig",
        "extensions": {"customer_note": "hi"},
    }
    out = embed_charter_in_mandate(original, "https://test/c")

    assert out["payer"] == "alice"
    assert out["payee"] == "bob"
    assert out["amount"] == {"value": 50.0, "currency": "USD"}
    assert out["signature"] == "sig"
    assert out["extensions"]["customer_note"] == "hi"
    assert out["extensions"]["charter_url"] == "https://test/c"


def test_embed_does_not_mutate_input():
    """embed returns a new dict; the caller's mandate is untouched."""
    original = {"payer": "a", "signature": "s", "extensions": {}}
    snapshot = {"payer": "a", "signature": "s", "extensions": {}}
    _ = embed_charter_in_mandate(original, "https://test/c")
    assert original == snapshot
    assert "charter_url" not in original["extensions"]


def test_embed_creates_extensions_when_absent():
    """If the mandate has no `extensions`, embed creates one."""
    mandate = {"payer": "a", "signature": "s"}
    out = embed_charter_in_mandate(mandate, "https://test/c")
    assert out["extensions"] == {"charter_url": "https://test/c"}


def test_embed_overwrites_existing_charter_url():
    mandate = {"extensions": {"charter_url": "https://old/c"}, "signature": "s"}
    out = embed_charter_in_mandate(mandate, "https://new/c")
    assert out["extensions"]["charter_url"] == "https://new/c"


def test_embed_rejects_non_dict_mandate():
    with pytest.raises(TypeError, match="dict"):
        embed_charter_in_mandate("not a dict", "https://test/c")  # type: ignore[arg-type]


def test_embed_rejects_empty_charter_url():
    with pytest.raises(ValueError, match="non-empty"):
        embed_charter_in_mandate({"signature": "s"}, "")


# ---------------------------------------------------------------------------
# verify — happy path
# ---------------------------------------------------------------------------


def test_verify_allows_when_both_layers_pass(fetch_charter_ok):
    """Mandate signature valid, Charter says allow -> final=allow."""
    grader = _grader([{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "in scope"}])
    mandate = _base_mandate()

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    assert isinstance(result, AP2VerifyResult)
    assert result.mandate_ok is True
    assert result.charter_verdict is not None
    assert result.charter_verdict.decision == "allow"
    assert result.final_decision == "allow"
    assert "passed" in result.reason


def test_verify_returns_needs_approval_when_charter_says_so(fetch_charter_ok):
    """Mandate ok + Charter=needs_approval -> final=needs_approval."""
    grader = _grader([{"id": "C-002", "hit": True, "confidence": 0.9, "reason": "large transfer"}])
    mandate = _base_mandate()

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    assert result.mandate_ok is True
    assert result.charter_verdict is not None
    assert result.charter_verdict.decision == "needs_approval"
    assert result.final_decision == "needs_approval"


def test_verify_incompatible_when_charter_blocks(fetch_charter_ok):
    """Mandate ok + Charter=incompatible -> final=incompatible."""
    grader = _grader([{"id": "C-003", "hit": True, "confidence": 0.95, "reason": "fraud"}])
    mandate = _base_mandate()

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    assert result.mandate_ok is True
    assert result.charter_verdict is not None
    assert result.charter_verdict.decision == "incompatible"
    assert result.final_decision == "incompatible"


# ---------------------------------------------------------------------------
# verify — failure modes
# ---------------------------------------------------------------------------


def test_verify_mandate_signature_missing_short_circuits(fetch_charter_ok):
    """Bad mandate signature -> final=incompatible, charter_verdict=None,
    fetcher is never called (no work wasted)."""
    fetch_called = {"count": 0}

    def fetch_spy(url: str) -> Charter:
        fetch_called["count"] += 1
        return fetch_charter_ok(url)

    grader_called = {"count": 0}

    def grader(_c: Charter, _t: str) -> list[dict[str, Any]]:
        grader_called["count"] += 1
        return []

    mandate = _base_mandate(signature="")  # missing signature

    result = verify(mandate, fetch_charter_fn=fetch_spy, hits_grader=grader)

    assert result.mandate_ok is False
    assert result.charter_verdict is None
    assert result.final_decision == "incompatible"
    assert fetch_called["count"] == 0
    assert grader_called["count"] == 0


def test_verify_mandate_signature_present_but_charter_allows_passes(fetch_charter_ok):
    """Even a non-empty signature counts as 'valid' in the mock; combined
    with Charter=allow this is the canonical success path."""
    grader = _grader([{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "ok"}])
    mandate = _base_mandate(signature="any-non-empty-blob")

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    assert result.mandate_ok is True
    assert result.final_decision == "allow"


def test_verify_bad_mandate_overrides_passing_charter(fetch_charter_ok):
    """The decision rule is asymmetric: a failed mandate forces
    incompatible even if Charter would otherwise say allow."""
    grader = _grader([{"id": "C-001", "hit": True, "confidence": 0.95, "reason": "in scope"}])
    mandate = _base_mandate(signature="")

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    # Charter wasn't even consulted; final still incompatible.
    assert result.mandate_ok is False
    assert result.charter_verdict is None
    assert result.final_decision == "incompatible"


def test_verify_charter_fetch_404_yields_incompatible():
    """Charter fetch fails -> mandate_ok=True but final=incompatible.

    This is the issue-mandated reverse case: a valid mandate referring to
    a missing Charter must be refused, not silently allowed."""

    def fetch_404(_url: str) -> Charter:
        raise CharterNotFoundError("not found")

    grader = _grader([{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "in scope"}])
    mandate = _base_mandate()

    result = verify(mandate, fetch_charter_fn=fetch_404, hits_grader=grader)

    assert result.mandate_ok is True
    assert result.charter_verdict is None
    assert result.final_decision == "incompatible"
    assert "CharterNotFoundError" in result.reason


def test_verify_charter_signature_error_yields_incompatible():
    """A forged or corrupted Charter -> same incompatible verdict."""

    def fetch_bad_sig(_url: str) -> Charter:
        raise CharterSignatureError("bad sig")

    mandate = _base_mandate()

    result = verify(mandate, fetch_charter_fn=fetch_bad_sig, hits_grader=_grader([]))

    assert result.mandate_ok is True
    assert result.charter_verdict is None
    assert result.final_decision == "incompatible"
    assert "CharterSignatureError" in result.reason


def test_verify_mandate_without_charter_url_is_refused(fetch_charter_ok):
    """A mandate with no extensions.charter_url is treated as
    incompatible — a Charter-aware verifier won't silently pass."""
    mandate = _base_mandate()
    mandate.pop("extensions")

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=_grader([]))

    assert result.mandate_ok is True
    assert result.charter_verdict is None
    assert result.final_decision == "incompatible"
    assert "charter_url" in result.reason


def test_verify_mandate_with_empty_charter_url_is_refused(fetch_charter_ok):
    mandate = _base_mandate()
    mandate["extensions"] = {"charter_url": ""}

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=_grader([]))

    assert result.final_decision == "incompatible"


# ---------------------------------------------------------------------------
# verify — task extraction
# ---------------------------------------------------------------------------


def test_verify_synthesizes_task_when_field_missing(fetch_charter_ok):
    """No explicit `task` field -> adapter synthesizes one from
    payer/payee/amount so the grader still has something to reason about."""
    captured_task: list[str] = []

    def grader(_c: Charter, task: str) -> list[dict[str, Any]]:
        captured_task.append(task)
        return [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "ok"}]

    mandate = _base_mandate()
    mandate.pop("task")

    result = verify(mandate, fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    assert result.final_decision == "allow"
    assert len(captured_task) == 1
    assert "alice@acme.com" in captured_task[0]
    assert "merchant@example.com" in captured_task[0]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_verify_emits_log_on_success(fetch_charter_ok, caplog):
    import logging

    grader = _grader([{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "ok"}])
    with caplog.at_level(logging.INFO, logger="charter.adapters.ap2"):
        verify(_base_mandate(), fetch_charter_fn=fetch_charter_ok, hits_grader=grader)

    rec = next(r for r in caplog.records if r.name == "charter.adapters.ap2")
    assert rec.final_decision == "allow"


def test_verify_emits_log_on_mandate_failure(fetch_charter_ok, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="charter.adapters.ap2"):
        verify(
            _base_mandate(signature=""),
            fetch_charter_fn=fetch_charter_ok,
            hits_grader=_grader([]),
        )

    rec = next(r for r in caplog.records if r.name == "charter.adapters.ap2")
    assert rec.final_decision == "incompatible"
    assert rec.mandate_ok is False


# ---------------------------------------------------------------------------
# AP2VerifyResult schema
# ---------------------------------------------------------------------------


def test_ap2_verify_result_serializes_round_trip():
    """The result model is plain pydantic; round-trip JSON for adapter
    consumers that log or persist it."""
    result = AP2VerifyResult(
        mandate_ok=True,
        charter_verdict=None,
        final_decision="incompatible",
        reason="test",
    )
    dumped = result.model_dump_json()
    parsed = AP2VerifyResult.model_validate_json(dumped)
    assert parsed == result
