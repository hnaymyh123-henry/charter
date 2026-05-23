"""Tests for charter.observability (B2.7).

Covers the four AC cases:

  1. OTel installed + tracer initialized -> spans are actually created
     (verified via the SDK's InMemorySpanExporter).
  2. OTel "not installed" -> decorator + context manager still callable,
     wrapped function still runs, no exception. Simulated by
     monkey-patching `_OTEL_AVAILABLE` to False (so this case works in
     CI even when the SDK is installed for tests).
  3. Key attributes (charter.id, charter.verdict, charter.principal_id,
     ...) actually land on the span.
  4. Exceptions inside a span produce status=ERROR and a recorded
     exception event.

Plus tests for the auxiliary contract: `functools.wraps` preservation,
async support, and attr sanitization.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from charter import observability
from charter.observability import (
    charter_span,
    charter_span_cm,
    init_tracer,
    is_otel_available,
    set_span_attrs,
)

# ---------------------------------------------------------------------------
# Fixture: an in-memory OTel pipeline so we can assert on span content.
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_exporter() -> Iterator[Any]:
    """Wire up an InMemorySpanExporter and yield it. Tears down provider
    state at the end so other tests aren't polluted."""
    pytest.importorskip("opentelemetry")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Replace the global tracer provider for the duration of the test.
    # OTel's ProxyTracerProvider lets us swap the underlying provider.
    original = trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]

    # Refresh the module's tracer cache so subsequent helper calls pick up
    # the new provider.
    observability._tracer = None
    init_tracer("charter-test")

    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = original  # type: ignore[attr-defined]
        observability._tracer = None


# ---------------------------------------------------------------------------
# AC #1 — real spans are produced when OTel is available
# ---------------------------------------------------------------------------


def test_decorator_creates_span_when_otel_installed(memory_exporter: Any) -> None:
    @charter_span("charter.test_op", {"charter.id": "abc"})
    def do_work(x: int) -> int:
        return x * 2

    result = do_work(3)
    assert result == 6

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "charter.test_op"


def test_context_manager_creates_span_when_otel_installed(memory_exporter: Any) -> None:
    with charter_span_cm("charter.test_cm", {"charter.id": "xyz"}) as span:
        span.set_attribute("charter.verdict", "ok")

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "charter.test_cm"


# ---------------------------------------------------------------------------
# AC #2 — no-OTel fallback: helpers are still callable and transparent
# ---------------------------------------------------------------------------


def test_decorator_is_noop_when_otel_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the 'opentelemetry not installed' branch without actually
    uninstalling the package. Asserts the decorator wraps cleanly, the
    function runs, return value is preserved, and no exception is raised
    even though no tracer ever existed."""
    monkeypatch.setattr(observability, "_OTEL_AVAILABLE", False)
    monkeypatch.setattr(observability, "_tracer", None)

    @charter_span("charter.test_noop", {"charter.id": "abc"})
    def do_work(x: int) -> int:
        return x + 1

    # functools.wraps must survive the no-op path too.
    assert do_work.__name__ == "do_work"

    result = do_work(41)
    assert result == 42

    # init_tracer returns None in the no-op path.
    assert init_tracer() is None
    assert is_otel_available() is False


def test_context_manager_is_noop_when_otel_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_OTEL_AVAILABLE", False)
    monkeypatch.setattr(observability, "_tracer", None)

    with charter_span_cm("charter.test_noop_cm", {"charter.id": "xyz"}) as span:
        # The yielded sentinel must accept set_attribute / set_status /
        # record_exception calls without raising.
        span.set_attribute("charter.verdict", "ok")
        span.set_attribute("charter.cache_hit", False)
        span.add_event("anything")
        assert span.is_recording() is False


# ---------------------------------------------------------------------------
# AC #3 — attributes land on the span correctly
# ---------------------------------------------------------------------------


def test_static_and_dynamic_attrs_set_on_span(memory_exporter: Any) -> None:
    """Static attrs from the decorator + dynamic attrs set via
    set_span_attrs / span.set_attribute should both end up on the
    exported span."""
    with charter_span_cm(
        "charter.test_attrs",
        {"charter.id": "charter:alice:bot:2026", "charter.principal_id": "alice@acme"},
    ) as span:
        set_span_attrs(
            span,
            {
                "charter.verdict": "allow",
                "charter.cache_hit": True,
                "charter.latency_ms": 42,
            },
        )

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["charter.id"] == "charter:alice:bot:2026"
    assert attrs["charter.principal_id"] == "alice@acme"
    assert attrs["charter.verdict"] == "allow"
    assert attrs["charter.cache_hit"] is True
    assert attrs["charter.latency_ms"] == 42


def test_non_primitive_attrs_are_dropped_silently(memory_exporter: Any) -> None:
    """If a caller accidentally passes a dict or a custom object as an
    attribute value, sanitize_attrs drops it rather than letting OTel
    raise. Observability must not become a new failure mode."""

    class CustomThing:
        pass

    with charter_span_cm(
        "charter.test_bad_attrs",
        {
            "charter.id": "ok",
            "charter.bad_dict": {"nested": "value"},  # type: ignore[dict-item]
            "charter.bad_obj": CustomThing(),  # type: ignore[dict-item]
            "charter.bad_list": [1, "mixed", object()],  # type: ignore[list-item]
        },
    ):
        pass

    spans = memory_exporter.get_finished_spans()
    attrs = dict(spans[0].attributes)
    assert "charter.id" in attrs
    assert "charter.bad_dict" not in attrs
    assert "charter.bad_obj" not in attrs
    assert "charter.bad_list" not in attrs


# ---------------------------------------------------------------------------
# AC #4 — exception path sets ERROR status and records the exception
# ---------------------------------------------------------------------------


def test_exception_sets_error_status_and_records(memory_exporter: Any) -> None:
    from opentelemetry.trace import StatusCode

    @charter_span("charter.test_raises")
    def boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        boom()

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    # The exception should appear as an event on the span.
    event_names = [e.name for e in span.events]
    assert "exception" in event_names


def test_exception_in_context_manager_also_records(memory_exporter: Any) -> None:
    from opentelemetry.trace import StatusCode

    with (
        pytest.raises(ValueError, match="cm fail"),
        charter_span_cm("charter.test_cm_raises", {"charter.id": "x"}),
    ):
        raise ValueError("cm fail")

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "exception" in [e.name for e in span.events]


# ---------------------------------------------------------------------------
# Aux: async + functools.wraps + tracer init contract
# ---------------------------------------------------------------------------


def test_decorator_supports_async_functions(memory_exporter: Any) -> None:
    @charter_span("charter.test_async", {"charter.id": "async"})
    async def do_async_work(x: int) -> int:
        # An await keeps the span context across the suspension point.
        await asyncio.sleep(0)
        return x * 3

    result = asyncio.run(do_async_work(4))
    assert result == 12

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "charter.test_async"


def test_decorator_preserves_wrapped_function_metadata() -> None:
    @charter_span("charter.test_wraps")
    def documented(x: int) -> int:
        """Original docstring."""
        return x

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Original docstring."


def test_init_tracer_idempotent_for_same_name(memory_exporter: Any) -> None:
    """init_tracer should be safe to call repeatedly. Tests that already
    use the fixture have one init; calling again here should not blow up."""
    t1 = init_tracer("charter-test")
    t2 = init_tracer("charter-test")
    assert t1 is not None
    assert t2 is not None


# ---------------------------------------------------------------------------
# Integration smoke: _fetch_and_verify and transparency.append emit spans
# ---------------------------------------------------------------------------


def test_fetch_and_verify_emits_span_on_failure(memory_exporter: Any) -> None:
    """When _fetch_and_verify raises (URL unreachable), the wrapping
    charter.fetch_and_verify span must still be exported with verdict
    set to the exception class name."""
    from charter.errors import CharterNotFoundError
    from charter.mcp_server import _fetch_and_verify

    with pytest.raises(CharterNotFoundError):
        _fetch_and_verify("http://127.0.0.1:1/this-port-is-closed/never")

    fetch_spans = [
        s for s in memory_exporter.get_finished_spans() if s.name == "charter.fetch_and_verify"
    ]
    assert len(fetch_spans) == 1
    attrs = dict(fetch_spans[0].attributes)
    # verdict carries the exception class name on failure
    assert attrs.get("charter.verdict") == "CharterNotFoundError"
    # latency was recorded
    assert "charter.latency_ms" in attrs


def test_transparency_append_emits_span(
    memory_exporter: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """transparency.append should emit a charter.transparency_append span
    with charter.id, charter.seq, and charter.verdict='appended' on the
    first append. Uses the same SIGNED-charter factory pattern as the
    existing transparency tests so we exercise the real append path
    (sign_charter -> append) end-to-end."""
    from datetime import UTC, datetime, timedelta

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
    from charter.signing import generate_keypair, public_key_to_string, sign_charter
    from charter.transparency import append

    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_PIN_FILE", str(tmp_path / "pins.json"))

    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id=f"charter:bob@acme.com:test_agent:{now.date().isoformat()}",
        binding=Binding(principal_id="bob@acme.com", agent_id="test_agent"),
        principal=Principal(id="bob@acme.com", role_summary="Test"),
        issuer=Issuer(id="bob@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Test."),
        clauses=[Clause(id="C-001", type="scope", text="test scope")],
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
    # sign_charter triggers transparency.append internally.
    sign_charter(charter, private)

    # A direct second call should be a no-op (duplicate verdict).
    memory_exporter.clear()
    entry = append(charter)
    assert entry.seq == 1

    append_spans = [
        s for s in memory_exporter.get_finished_spans() if s.name == "charter.transparency_append"
    ]
    assert len(append_spans) == 1
    attrs = dict(append_spans[0].attributes)
    assert attrs["charter.id"] == charter.charter_id
    assert attrs["charter.seq"] == 1
    assert attrs["charter.verdict"] == "duplicate"
