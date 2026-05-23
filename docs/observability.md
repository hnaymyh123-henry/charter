# Observability — OpenTelemetry semantic conventions

Charter emits OpenTelemetry spans on every protocol-layer hot path so
your deployment can plug its existing OTel pipeline (Datadog, Honeycomb,
Tempo, Jaeger, Grafana Cloud, ...) and see Charter activity inline with
the rest of your agent stack.

OTel is an **optional dependency**. Without it installed, every span
helper in `charter.observability` silently degrades to a no-op — your
production import path never breaks and no runtime cost is paid.

## Install

```bash
pip install charter[observability]
```

That pulls in `opentelemetry-api>=1.20` and `opentelemetry-sdk>=1.20`.

## Quick start

```python
from charter.observability import init_tracer

# Call once at process start. Returns the Tracer, or None if OTel
# isn't installed. Production deployments usually configure their own
# TracerProvider beforehand and this call just wires Charter into it.
init_tracer("charter")

# ... your normal Charter code. Spans are emitted automatically on
# every instrumented call (no further changes needed at call sites).
```

If you have not configured a `TracerProvider`, OTel ships a no-op
default — spans are created but go nowhere. To actually export, wire
up your exporter of choice (see [Deployment examples](#deployment-examples)).

## Span schema

All Charter span names live under the `charter.*` namespace. All
attribute keys also use the `charter.*` namespace — these are
**Charter-private** semantic conventions, NOT official OpenTelemetry
semconv (we do not impersonate `http.*`, `gen_ai.*`, or any other
established vocabulary).

| Span name | Emitted by | Attributes |
|---|---|---|
| `charter.fetch_and_verify` | `mcp_server._fetch_and_verify` | `charter.url`, `charter.id`, `charter.principal_id`, `charter.agent_id`, `charter.verdict` (`ok` or exception class), `charter.cache_hit`, `charter.latency_ms` |
| `charter.fetch_chain` | `mcp_server.fetch_charter_chain` | `charter.url`, `charter.max_depth`, `charter.chain_depth`, `charter.chain_root_id`, `charter.chain_leaf_id`, `charter.verdict` (`ok`, `max_depth_exceeded`, `cycle`, `attenuation_broken`, exception class) |
| `charter.aggregate_chain` | `mcp_server.aggregate_verdict_chain` | `charter.chain_depth`, `charter.matched_clause_count`, `charter.applied_clause_count`, `charter.verdict` (final decision string) |
| `charter.verify_chain` | `chain.verify_chain` | `charter.id`, `charter.parent_id`, `charter.mode` (`strict`/`semantic`/`auto`), `charter.verdict` (`ok` or `rejected`), `charter.via` (in auto mode only: `strict` or `semantic`) |
| `charter.fetch_jwks` | `keys.fetch_jwks` | `charter.issuer_origin`, `charter.jwks_cache_hit`, `charter.jwks_key_count`, `charter.verdict` |
| `charter.transparency_append` | `transparency.append` | `charter.id`, `charter.seq`, `charter.verdict` (`appended` or `duplicate`) |

### Attribute conventions

- **`charter.id`** — the Charter's `charter_id` (a stable string).
- **`charter.principal_id`** — `charter.binding.principal_id`, e.g.
  `alice@acme.com`.
- **`charter.agent_id`** — `charter.binding.agent_id`, e.g.
  `research_agent_v1`.
- **`charter.verdict`** — the outcome. For success paths this is
  usually `"ok"` (or for aggregation, the literal decision string
  `"allow"` / `"needs_approval"` / `"incompatible"`). For failure
  paths it carries the typed-exception class name
  (`"CharterNotFoundError"`, `"CharterPinMismatchError"`, etc.) so
  dashboards can group errors by class.
- **`charter.cache_hit`** — `true` iff the call short-circuited via a
  cache.
- **`charter.latency_ms`** — integer milliseconds spent in the wrapped
  call. Useful for SLO dashboards.

All values are restricted to OTel primitives (str / int / float / bool
or sequences of those). The helper sanitizes attrs before setting and
drops any value that isn't a primitive, so a misuse of the
`set_span_attrs` helper never crashes the instrumented call.

### Errors

When an instrumented function raises, the wrapping helper:

1. Calls `span.record_exception(e)` so the exception lands as a span
   event with full traceback.
2. Calls `span.set_status(Status(StatusCode.ERROR, str(e)))` so APMs
   show the span as red.
3. Re-raises so behavior is unchanged for callers.

This means in Datadog / Honeycomb / Tempo the failing Charter call
appears as a red trace with the exception class name on `charter.verdict`
and the full Python traceback as a span event.

## Deployment examples

The three exporters below are the most common targets. Pick one. All
of them follow the same pattern: install the exporter package, build a
`TracerProvider` with a `BatchSpanProcessor`, register it as the
global provider, then call `init_tracer("charter")`.

### Jaeger / Tempo (OTLP)

Most modern OSS tracing backends speak OTLP. This is the most portable
option.

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from charter.observability import init_tracer

resource = Resource.create({"service.name": "charter"})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(endpoint="http://tempo.internal:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

init_tracer("charter")
```

Install: `pip install opentelemetry-exporter-otlp-proto-grpc`.

### Datadog

Datadog accepts OTel-format spans natively when you point the OTLP
exporter at the Datadog agent's OTLP receiver (default port 4317):

```python
import os
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from charter.observability import init_tracer

resource = Resource.create({
    "service.name": "charter",
    "deployment.environment": os.environ.get("ENV", "production"),
})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(endpoint="http://datadog-agent:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

init_tracer("charter")
```

Enable OTLP in your Datadog agent config (`datadog.yaml`):

```yaml
otlp_config:
  receiver:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
```

### Honeycomb

Honeycomb's OTLP endpoint accepts spans directly with an API key:

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from charter.observability import init_tracer

resource = Resource.create({"service.name": "charter"})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(
    endpoint="https://api.honeycomb.io/v1/traces",
    headers={"x-honeycomb-team": "YOUR_API_KEY"},
)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

init_tracer("charter")
```

Install: `pip install opentelemetry-exporter-otlp-proto-http`.

## Relationship to other Charter observability surfaces

Charter already emits **structured logs** through `charter._logging`
on every fetch / verify outcome. Those logs use the same field names
where they overlap (`charter_id`, `principal_id`, `outcome`), but the
JSON line format and the OTel span format are independent —
configuring one does not affect the other. Most deployments will use
both: logs for free-form debugging and queries, traces for latency /
flame-graphs / error grouping.

There is no metrics surface yet. Metrics could land on top of these
spans (latency histograms grouped by `charter.verdict`) but that's
left to the deployment side rather than baked in.

## Relationship to official OpenTelemetry semantic conventions

The `charter.*` attribute namespace is **deliberately private**.

OpenTelemetry's official semconv covers HTTP, RPC, databases, AI
inference (`gen_ai.*`), and a few other domains. Authorization /
delegation has no published OTel semconv yet, and Charter does not
claim to ship one. If a future OTel semconv standardizes
delegation-protocol attributes, we will map `charter.*` to those names
in a follow-up — but the present version stays Charter-private to
avoid pretending to a standard that does not exist.

## Reference

- Module: [`charter/observability.py`](../charter/observability.py)
- Tests: [`tests/test_observability.py`](../tests/test_observability.py)
- Optional dep: `charter[observability]` in `pyproject.toml`
- Issue: [#40 — B2.7 Observability standard](https://github.com/hnaymyh123-henry/charter/issues/40)
