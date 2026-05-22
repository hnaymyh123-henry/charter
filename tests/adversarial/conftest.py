"""Shared fixtures for the adversarial test suite.

Design intent:

  - Crypto is REAL. Adversarial crypto tests must exercise the actual
    Ed25519 sign / verify paths; mocking them would only prove the mock
    behaves correctly.

  - LLM is FAKE. `FakeAnthropicClient` deterministically returns canned
    responses based on which prompt template (`system`) was used and a
    user-controllable scenario knob. This lets a test simulate both an
    "honest grader" (returns the truthful per-clause hits) and a
    "compromised grader" (returns whatever a successful prompt injection
    would yield) without ever talking to the network.

  - Filesystem state is per-test isolated. `CHARTER_DATA_DIR`,
    `CHARTER_PIN_FILE` and `CHARTER_TRANSPARENCY_LOG` are all rerouted to
    `tmp_path`. JWKS cache is cleared autouse so a test that stubs a
    different JWKS body across two fetches sees the second one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from charter import keys as keys_mod
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
from charter.signing import generate_keypair, public_key_to_string, sign_charter

# ---------------------------------------------------------------------------
# Filesystem isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute every persistent file Charter writes to a per-test tmp dir.

    Covers `data/charters/`, `data/keys/`, the pin file, and the
    transparency log. Without this, adversarial tests that exercise
    pinning / log append leak into the real `data/` directory.
    """
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))
    monkeypatch.setenv("CHARTER_TRANSPARENCY_LOG", str(tmp_path / "transparency.log"))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_jwks_cache() -> Any:
    """Drop the JWKS module's in-process cache around each test.

    Tests that stub one JWKS body, fetch once, then stub a different
    body and fetch again would otherwise see a stale cache hit and
    miss the attack scenario entirely.
    """
    keys_mod.clear_cache()
    yield
    keys_mod.clear_cache()


# ---------------------------------------------------------------------------
# Charter factory — real Ed25519 keys, customizable clauses
# ---------------------------------------------------------------------------


def _build_charter(
    *,
    private: Any = None,
    public: Any = None,
    principal_id: str = "alice@acme.com",
    agent_id: str = "research_agent_v1",
    charter_id: str | None = None,
    clauses: list[Clause] | None = None,
    parent_charter_url: str | None = None,
    attenuation_proof: AttenuationProof | None = None,
) -> Charter:
    """Build and sign a Charter. Returns the signed object.

    Caller may supply a keypair to control the signing identity (used by
    crypto-attack tests). Otherwise a fresh keypair is generated.
    """
    if private is None or public is None:
        private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    cid = charter_id or f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}"
    charter = Charter(
        charter_id=cid,
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Test principal"),
        issuer=Issuer(id=principal_id),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Adversarial test charter."),
        clauses=clauses
        or [
            Clause(id="C-001", type="scope", text="Accounting and tax work."),
            Clause(id="C-002", type="out_of_scope", text="Marketing copy."),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="adversarial test",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
        parent_charter_url=parent_charter_url,
        attenuation_proof=attenuation_proof,
    )
    sign_charter(charter, private)
    return charter


@pytest.fixture
def signed_charter(temp_data_dir: Path) -> Charter:  # noqa: ARG001
    """A vanilla signed Charter with two clauses (scope + out_of_scope).

    Depends on `temp_data_dir` so `sign_charter`'s transparency-log write
    lands in the per-test directory.
    """
    return _build_charter()


@pytest.fixture
def charter_factory(temp_data_dir: Path) -> Callable[..., Charter]:  # noqa: ARG001
    """Return a callable that builds Charters with caller-supplied overrides.

    Most adversarial tests need a Charter with adversarial clause text or
    a specific keypair — using the factory keeps each test's surface area
    small and explicit.
    """

    def _factory(**kwargs: Any) -> Charter:
        return _build_charter(**kwargs)

    return _factory


# ---------------------------------------------------------------------------
# FakeAnthropicClient — deterministic LLM stub for adversarial tests
# ---------------------------------------------------------------------------


class _FakeContentBlock:
    """Mimics anthropic's content block (only the bits we read)."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContentBlock(text)]


class FakeAnthropicMessages:
    """Records every `create()` call so tests can assert on the prompts."""

    def __init__(self, responder: Callable[[dict[str, Any]], str]) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return _FakeMessage(self._responder(kwargs))


class FakeAnthropicClient:
    """Stand-in for `anthropic.Anthropic()` driven by a responder callable.

    Tests pass a `responder(kwargs) -> str` that inspects the prompt
    (system text + user content) and returns whatever JSON the test wants
    the "LLM" to have produced. This is the seam where adversarial tests
    inject `compromised` or `honest` grader behavior.
    """

    def __init__(self, responder: Callable[[dict[str, Any]], str]) -> None:
        self.messages = FakeAnthropicMessages(responder)


@pytest.fixture
def fake_anthropic_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[dict[str, Any]], str]], FakeAnthropicClient]:
    """Return a factory that installs a FakeAnthropicClient for a test.

    Usage in a test:

        def test_x(fake_anthropic_factory):
            client = fake_anthropic_factory(lambda kw: '{"hits": []}')
            ...  # code that calls anthropic.Anthropic() will get `client`

    Sets a fake `ANTHROPIC_API_KEY` so call sites that check for the key
    don't bail early; the actual key value is irrelevant — no network
    call is ever made.
    """
    state: dict[str, FakeAnthropicClient | None] = {"client": None}

    def install(responder: Callable[[dict[str, Any]], str]) -> FakeAnthropicClient:
        client = FakeAnthropicClient(responder)
        state["client"] = client

        def _factory(*args: Any, **kwargs: Any) -> FakeAnthropicClient:
            return client

        # Patch the two modules that hold an `import anthropic` reference.
        # Both `propose` and `loopback` create their own client.
        monkeypatch.setattr("charter.propose.anthropic.Anthropic", _factory)
        monkeypatch.setattr("charter.loopback.anthropic.Anthropic", _factory)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-adversarial-test")
        return client

    return install


# ---------------------------------------------------------------------------
# Helper — call FastMCP-decorated tools by their underlying function
# ---------------------------------------------------------------------------


def call_mcp_tool(tool: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a `@mcp.tool()`-decorated function under its real callable.

    FastMCP wraps the original callable; older versions stored it as
    `fn` / `func`; some store it as `__wrapped__`. This helper unwraps
    once and falls through to calling the wrapper directly if no
    attribute matches.
    """
    for attr in ("fn", "func", "__wrapped__"):
        if hasattr(tool, attr):
            return getattr(tool, attr)(*args, **kwargs)
    return tool(*args, **kwargs)
