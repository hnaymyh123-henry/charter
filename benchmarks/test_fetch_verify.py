"""Benchmark `_fetch_and_verify` end-to-end with HTTP stubbed.

What we time:
    The signature verification + JWKS cross-check + pin update path.
    HTTP is stubbed so the number reflects in-process verification
    cost, not network latency. The CI runner's TLS + DNS overhead is
    not the bottleneck operators care about — they care about how much
    of *their* request budget the protocol burns.

The `stub_http_get` fixture from `conftest.py` routes BOTH the
Charter URL and the JWKS `/.well-known/jwks.json` URL through one
in-memory map; each test publishes both endpoints up front so
neither hits the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from charter.keys import issuer_origin_from_url
from charter.mcp_server import _fetch_and_verify
from charter.signing import kid_for_public_key, public_key_to_jwk, verify_charter

from ._factories import make_signed_charter


def _publish(
    store: dict[str, Any],
    charter_url: str,
    charter: Any,
) -> None:
    """Add Charter + matching JWKS entry to the stub store."""
    store[charter_url] = charter.model_dump(mode="json")

    pk = charter.provenance.issuer_public_key
    kid = kid_for_public_key(pk)
    jwk = public_key_to_jwk(pk, kid=kid)
    jwks_url = f"{issuer_origin_from_url(charter_url)}/.well-known/jwks.json"
    store[jwks_url] = {"keys": [jwk]}


@pytest.fixture
def hosted_charter(stub_http_get: dict[str, Any]) -> str:
    """Publish a small (4-clause) Charter + its issuer JWKS, return the URL."""
    charter, _ = make_signed_charter()
    url = "http://bench.local/p/a"
    _publish(stub_http_get, url, charter)
    return url


@pytest.fixture
def hosted_charter_large(stub_http_get: dict[str, Any]) -> str:
    """Publish a 32-clause Charter at a distinct URL."""
    charter, _ = make_signed_charter(
        charter_id="charter:p:large:2026-05-23",
        n_clauses=32,
    )
    url = "http://bench.local/p/a-large"
    _publish(stub_http_get, url, charter)
    return url


def test_bench_fetch_and_verify(benchmark: Any, hosted_charter: str) -> None:
    """`_fetch_and_verify` p50/p99 for a small Charter (4 clauses).

    The pin-check path takes the WRITE branch on the first call (TOFU)
    and the READ branch on every subsequent call. Most operator
    workloads are dominated by the READ branch, which is what
    pytest-benchmark's averaged rounds measure.

    The JWKS cache is cold on the first round (because the autouse
    `_clear_jwks_cache` fixture resets it per-test) and warm
    thereafter — so the recorded mean conflates one miss with N-1
    hits. The JWKS-specific cost is teased apart in
    `test_jwks_cache.py`.
    """

    def run() -> None:
        _fetch_and_verify(hosted_charter)

    benchmark(run)


def test_bench_fetch_and_verify_large_clauseset(benchmark: Any, hosted_charter_large: str) -> None:
    """Same path with a 32-clause Charter — verification cost scales
    with `len(clauses)` because canonical-bytes serialization walks
    the whole payload.
    """

    def run() -> None:
        _fetch_and_verify(hosted_charter_large)

    benchmark(run)


def test_bench_verify_signature_only(benchmark: Any) -> None:
    """Time JUST `verify_charter` (signature verification path) for
    a small Charter. Subtract this from `_fetch_and_verify` to
    isolate the trust-model overhead (JWKS cross-check + pin).
    """
    charter, _ = make_signed_charter()

    def run() -> None:
        assert verify_charter(charter) is True

    benchmark(run)
