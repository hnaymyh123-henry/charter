"""Benchmark JWKS cache hit vs miss.

The "miss" path includes parsing the JWKS body. The "hit" path is a
single dict lookup + monotonic-time comparison; if the gap between the
two numbers ever collapses to noise, it is a signal the cache was
silently bypassed (e.g. someone added cache invalidation in the hot
path).
"""

from __future__ import annotations

from typing import Any

import pytest

from charter import keys as keys_mod
from charter.keys import fetch_jwks
from charter.signing import generate_keypair, public_key_to_jwk, public_key_to_string


def _build_jwks_payload(num_keys: int = 3) -> dict[str, Any]:
    """A JWKS document with `num_keys` Ed25519 entries.

    Real issuers usually publish 1-3 keys (current + last rotation +
    upcoming). 3 is a realistic upper bound.
    """
    keys = []
    for i in range(num_keys):
        _, public = generate_keypair()
        jwk = public_key_to_jwk(public_key_to_string(public), kid=f"bench-kid-{i}")
        keys.append(jwk)
    return {"keys": keys}


@pytest.fixture
def stub_jwks(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `httpx.get` used by `charter.keys.fetch_jwks`."""
    payload = _build_jwks_payload(num_keys=3)

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return payload

    def fake_get(url: str, **_kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr("httpx.get", fake_get)
    return payload


def test_bench_jwks_cache_miss(benchmark: Any, stub_jwks: dict[str, Any]) -> None:
    """Cold cache → parse JWKS body. Cache cleared between iterations
    so every round measures the parse path."""

    def run() -> None:
        keys_mod.clear_cache()
        fetch_jwks("http://issuer.bench.local")

    benchmark(run)


def test_bench_jwks_cache_hit(benchmark: Any, stub_jwks: dict[str, Any]) -> None:
    """Warm cache → dict lookup + TTL check. Cache primed once before
    the benchmark loop starts."""
    fetch_jwks("http://issuer.bench.local")  # prime

    def run() -> None:
        fetch_jwks("http://issuer.bench.local")

    benchmark(run)


def test_bench_jwks_fetch_with_decode(benchmark: Any, stub_jwks: dict[str, Any]) -> None:
    """Cold cache + downstream `jwk_to_public_key_string` — closer to
    what `_check_jwks_consistency` actually executes."""

    def run() -> None:
        keys_mod.clear_cache()
        jwks = fetch_jwks("http://issuer.bench.local")
        # Mimic the cross-check path: pick one entry and decode it.
        first_kid = next(iter(jwks))
        keys_mod.jwk_to_public_key_string(jwks[first_kid])

    benchmark(run)
