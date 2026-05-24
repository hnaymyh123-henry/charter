"""OpenTelemetry semantic conventions for Charter (B2.7).

Charter emits spans on every protocol-layer hot path so deployments can
plug their existing OpenTelemetry pipeline (Datadog, Honeycomb, Tempo,
Jaeger, Grafana Cloud, ...) and see Charter activity alongside the rest
of their agent stack. OTel is intentionally an **optional dependency**:

  - With `pip install charter[observability]`, real OTel spans are
    created on every instrumented call and exported through whatever
    `TracerProvider` the deployment has configured.
  - Without OTel installed, every helper in this module silently
    degrades to a no-op so production import-paths never break and no
    runtime cost is paid.

Span naming is `charter.<verb>` (e.g. `charter.fetch_and_verify`).
Attributes use a private `charter.*` namespace (`charter.id`,
`charter.principal_id`, `charter.verdict`, ...) so we never claim to
ship official OpenTelemetry semconv — there is no `http.charter.*` or
`gen_ai.charter.*` pretender. Full schema in `docs/observability.md`.

Three public surfaces:

  - `init_tracer(service_name="charter") -> Tracer | None`
      Explicit initialization for callers that want a one-shot setup.
      Returns the tracer (or `None` when OTel is missing). Production
      callers usually let their own deployment set up the
      `TracerProvider`; this helper is mostly for tests and for the
      "I just want to see traces locally" path.

  - `@charter_span(name, attrs=None)`
      Decorator for sync OR async functions. Wraps the call in a span
      named `name`, sets the static `attrs` on entry, records any
      exception + sets ERROR status if the wrapped function raises.

  - `charter_span_cm(name, attrs=None)`
      Context-manager equivalent for code that isn't a function (e.g.
      part of a longer block). Yields the underlying span so callers
      can `set_attribute` dynamic values mid-flight.

Both helpers preserve `functools.wraps` semantics — `__name__` /
`__doc__` survive.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar, cast

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the no-OTel test path
    _otel_trace = None  # type: ignore[assignment,unused-ignore]
    Status = None  # type: ignore[assignment,misc,unused-ignore]
    StatusCode = None  # type: ignore[assignment,misc,unused-ignore]
    _OTEL_AVAILABLE = False


F = TypeVar("F", bound=Callable[..., Any])

# Module-level tracer cache. `init_tracer` populates it; `_get_tracer`
# falls back to the global default tracer when not initialized.
_tracer: Any = None


def is_otel_available() -> bool:
    """True iff the `opentelemetry` package is importable.

    Tests use this to skip the real-span assertions and only run the
    no-op contract checks. Production code should never need to branch
    on this — the helpers below already handle both cases.
    """
    return _OTEL_AVAILABLE


def init_tracer(service_name: str = "charter") -> Any:
    """Initialize and return Charter's OTel `Tracer`.

    Behavior:
        - If OTel is installed, returns a `Tracer` named for
          `service_name`. The first call caches the tracer module-wide.
          Subsequent calls return the same tracer for the same name; a
          different name produces a fresh tracer (and overwrites the
          cache).
        - If OTel is NOT installed, returns `None` and prints nothing.
          Decorator + context-manager APIs continue to work as no-ops.

    Production deployments typically configure their own
    `TracerProvider` (with an OTLP exporter to Datadog / Tempo /
    Honeycomb) *before* importing charter; this function then just
    wires charter into that already-set-up pipeline. The default OTel
    `TracerProvider` is a no-op, so calling this without any further
    setup is also safe — spans are created but go nowhere.
    """
    global _tracer
    if not _OTEL_AVAILABLE:
        return None
    _tracer = _otel_trace.get_tracer(service_name)
    return _tracer


def _get_tracer() -> Any:
    """Internal: return the configured tracer, lazily initializing if
    needed. Returns `None` when OTel is unavailable."""
    if not _OTEL_AVAILABLE:
        return None
    if _tracer is None:
        return _otel_trace.get_tracer("charter")
    return _tracer


def _sanitize_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Drop entries whose values are not OTel-allowed primitives.

    OTel only allows str/int/float/bool (and sequences of those) as
    attribute values. Anything else (dicts, custom objects) is dropped
    silently so a misuse of this module never crashes the wrapped
    function — observability must not become a new failure mode.
    """
    if not attrs:
        return {}
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)):
            out[k] = v
        elif isinstance(v, (list, tuple)) and all(
            isinstance(x, (str, bool, int, float)) for x in v
        ):
            out[k] = list(v)
        # else: silently drop
    return out


@contextmanager
def charter_span_cm(name: str, attrs: dict[str, Any] | None = None) -> Iterator[Any]:
    """Context-manager span for non-function scopes.

    Yields the underlying OTel `Span` so callers can do
    `span.set_attribute("charter.cache_hit", True)` mid-flight. When
    OTel is unavailable, yields a sentinel object whose `set_attribute`
    / `record_exception` / `set_status` calls all silently no-op.

    Usage:

        with charter_span_cm("charter.fetch_and_verify",
                             {"charter.id": cid}) as span:
            ...
            span.set_attribute("charter.verdict", "allow")

    Exceptions raised inside the `with` block are recorded on the span
    (when OTel is available) and the span status is set to ERROR before
    the exception propagates.
    """
    tracer = _get_tracer()
    if tracer is None:
        yield _NoopSpan()
        return

    with tracer.start_as_current_span(name) as span:
        for k, v in _sanitize_attrs(attrs).items():
            span.set_attribute(k, v)
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def charter_span(name: str, attrs: dict[str, Any] | None = None) -> Callable[[F], F]:
    """Decorator factory: wrap a function (sync or async) in a span.

    Args:
        name:  The span name. Use `charter.<verb>`, e.g.
               `charter.fetch_and_verify`.
        attrs: Optional dict of static attributes to set on entry.
               Keys should be under the `charter.*` namespace
               (`charter.id`, `charter.principal_id`, ...).

    The wrapper:
      - Preserves `__name__` / `__doc__` via `functools.wraps`.
      - Detects coroutine functions automatically via
        `inspect.iscoroutinefunction` and returns an awaitable wrapper
        in that case. The span context propagates across `await`
        because we use OTel's `start_as_current_span` (which uses
        contextvars under the hood).
      - On exception: records the exception on the span, sets status
        to ERROR, re-raises.
      - When OTel is missing: returns the wrapped function essentially
        unchanged (still preserving __name__/__doc__, still passing
        through args / return value / exceptions).
    """

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                tracer = _get_tracer()
                if tracer is None:
                    return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)
                with tracer.start_as_current_span(name) as span:
                    for k, v in _sanitize_attrs(attrs).items():
                        span.set_attribute(k, v)
                    try:
                        return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)
                    except Exception as e:
                        span.record_exception(e)
                        span.set_status(Status(StatusCode.ERROR, str(e)))
                        raise

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = _get_tracer()
            if tracer is None:
                return func(*args, **kwargs)
            with tracer.start_as_current_span(name) as span:
                for k, v in _sanitize_attrs(attrs).items():
                    span.set_attribute(k, v)
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    raise

        return cast(F, sync_wrapper)

    return decorator


def set_span_attrs(span: Any, attrs: dict[str, Any]) -> None:
    """Set multiple attributes on an active span. No-op on no-op spans.

    Use this inside a `charter_span_cm(...) as span:` block when you
    need to set dynamic attributes that weren't known at span creation
    time (e.g. `charter.verdict` after aggregation runs, or
    `charter.cache_hit` after a cache lookup).
    """
    if span is None:
        return
    sanitized = _sanitize_attrs(attrs)
    setter = getattr(span, "set_attribute", None)
    if setter is None:
        return
    for k, v in sanitized.items():
        setter(k, v)


class _NoopSpan:
    """Inert span returned when OTel is missing or no tracer is set.

    Mirrors enough of the OTel `Span` interface that instrumented call
    sites can use `span.set_attribute(...)` / `span.record_exception(...)`
    / `span.set_status(...)` without branching on whether OTel is
    available. Every method is intentionally a no-op.
    """

    def set_attribute(self, key: str, value: Any) -> None:
        return

    def set_attributes(self, attrs: dict[str, Any]) -> None:
        return

    def record_exception(self, exception: BaseException) -> None:
        return

    def set_status(self, status: Any, description: str | None = None) -> None:
        return

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return

    def end(self) -> None:
        return

    def is_recording(self) -> bool:
        return False
