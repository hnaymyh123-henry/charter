"""Factories + fake grader shared across the benchmark suite.

Lives in its own module (not `conftest.py`) so individual benchmark
files can do `from benchmarks._factories import make_signed_charter`
without relying on conftest auto-loading semantics.

All factories are deterministic: every call re-seeds Python's
`random` with the suite-wide seed, so the *shape* of the generated
Charter (and therefore the canonical-bytes payload signed) never
drifts between runs. Per-call key generation still produces fresh
Ed25519 keypairs because `cryptography` uses OS randomness directly,
but the test fixtures don't care about the key VALUE — only that the
sign/verify round-trips.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from charter.schema import (
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
from charter.signing import (
    Ed25519PrivateKey,
    generate_keypair,
    public_key_to_string,
    sign_charter,
)

SEED = 42


def _now() -> datetime:
    """Anchor datetime — fixed string parsing to avoid wall-clock drift."""
    return datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)


def _clause(cid: str, ctype: str, text: str) -> Clause:
    return Clause(id=cid, type=ctype, text=text)  # type: ignore[arg-type]


def make_signed_charter(
    *,
    charter_id: str = "charter:p:a:2026-05-23",
    principal_id: str = "p",
    agent_id: str = "a",
    n_clauses: int = 4,
    parent_charter_url: str | None = None,
    parent_charter_id: str | None = None,
    issued_at: datetime | None = None,
) -> tuple[Charter, Ed25519PrivateKey]:
    """Build + sign a Charter deterministically.

    Returns `(charter, issuer_private_key)`. The private key is needed
    by benchmarks that mutate the Charter and want to re-sign.
    """
    random.seed(SEED)
    private, public = generate_keypair()
    now = (issued_at or _now()).replace(microsecond=0)

    clause_types = ["scope", "out_of_scope", "approval_required", "operational_limit"]
    clauses = [
        _clause(
            cid=f"C-{i:03d}",
            ctype=clause_types[i % len(clause_types)],
            text=f"Clause {i} synthetic text for benchmark deterministic seed.",
        )
        for i in range(n_clauses)
    ]

    proof = (
        AttenuationProof(parent_charter_id=parent_charter_id)
        if parent_charter_id is not None
        else None
    )

    charter = Charter(
        charter_id=charter_id,
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Benchmark principal."),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Benchmark charter."),
        clauses=clauses,
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="benchmark",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
        parent_charter_url=parent_charter_url,
        attenuation_proof=proof,
    )
    sign_charter(charter, private)
    return charter, private


def make_chain(
    depth: int,
    *,
    base_url: str = "http://bench.local",
) -> list[tuple[Charter, Ed25519PrivateKey]]:
    """Build a `depth`-long valid Charter chain, root-first."""
    if depth < 1:
        raise ValueError("depth must be >= 1")

    shared_clauses: list[tuple[str, str, str]] = [
        ("C-001", "scope", "Engineering work."),
        ("C-002", "out_of_scope", "Do not write production database migrations."),
        ("C-003", "approval_required", "Production deploys require human approval."),
    ]

    chain: list[tuple[Charter, Ed25519PrivateKey]] = []
    for i in range(depth):
        is_root = i == 0
        parent_url = None if is_root else f"{base_url}/p/a{i - 1}"
        parent_id = None if is_root else f"charter:p:a{i - 1}:bench"
        private, public = generate_keypair()
        now = _now().replace(microsecond=0)
        clauses = [_clause(cid, ctype, text) for cid, ctype, text in shared_clauses]
        proof = AttenuationProof(parent_charter_id=parent_id) if parent_id is not None else None
        charter = Charter(
            charter_id=f"charter:p:a{i}:bench",
            binding=Binding(principal_id="p", agent_id=f"a{i}"),
            principal=Principal(id="p", role_summary="Chain principal."),
            issuer=Issuer(id="p"),
            agent_operator=AgentOperator(id="generic"),
            summary=Summary(plain_language="Chain link."),
            clauses=clauses,
            lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
            provenance=Provenance(
                issuer_public_key=public_key_to_string(public),
                issuer_signature="",
                source_commitments=[
                    SourceCommitment(
                        type="profile_yaml",
                        description="bench-chain",
                        content_hash="sha256:" + "0" * 64,
                    )
                ],
                generated_at=now,
            ),
            parent_charter_url=parent_url,
            attenuation_proof=proof,
        )
        sign_charter(charter, private)
        chain.append((charter, private))

    return chain


# ---------------------------------------------------------------------------
# Fake grader (canned-response or sleep-based)
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Minimal Anthropic-style message stand-in."""

    def __init__(self, text: str) -> None:
        class _Block:
            pass

        block = _Block()
        block.type = "text"  # type: ignore[attr-defined]
        block.text = text  # type: ignore[attr-defined]
        self.content = [block]


class _FakeMessages:
    """Implements the `.messages.create(...)` shape `charter.chain` calls."""

    def __init__(self, response_text: str, *, latency_s: float = 0.0) -> None:
        self._response_text = response_text
        self._latency_s = latency_s
        self.call_count = 0

    def create(self, **_kwargs: Any) -> _FakeMessage:
        self.call_count += 1
        if self._latency_s > 0:
            time.sleep(self._latency_s)
        return _FakeMessage(self._response_text)


class FakeGrader:
    """Drop-in `GraderClient` for chain-semantic / grader benchmarks.

    `latency_s` simulates LLM round-trip time so the grader-latency
    benchmark can chart how chain depth scales against per-call cost.
    """

    def __init__(
        self,
        response_text: str = '{"matches_subset": true, "reason": "covered"}',
        *,
        latency_s: float = 0.0,
    ) -> None:
        self.messages = _FakeMessages(response_text, latency_s=latency_s)
