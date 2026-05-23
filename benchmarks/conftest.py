"""Shared pytest fixtures for the perf-baseline benchmark suite (Issue #43).

Design intent:

  - Every factory uses `random.seed(42)` so the dataset itself never
    drifts between runs (see `_factories.py`). The only timing variance
    comes from the machine; pytest-benchmark autotunes rounds/iterations.

  - File-system state is sandboxed via `tmp_path` plus `monkeypatch`
    overrides of `CHARTER_DATA_DIR` and `CHARTER_TRANSPARENCY_LOG`.
    Pytest cleans `tmp_path` per test, so no benchmark leaks state.

  - JWKS cache is cleared between tests so cache-hit vs cache-miss
    benchmarks measure what their names claim.

  - No real HTTP / LLM calls anywhere. The grader latency benchmark
    uses a sleeping fake; the real Anthropic measurements live in
    `docs/performance.md` as a separately-maintained reference table.
"""

from __future__ import annotations

import random
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from charter import keys as keys_mod

from ._factories import SEED, FakeGrader

__all__ = ["FakeGrader"]


def pytest_configure(config: pytest.Config) -> None:
    """Register the `live` marker (used by `test_grader_latency.py`).

    Real LLM calls are skipped by default; with `pytest -m live` and
    `ANTHROPIC_API_KEY` set, they run. Registering here avoids the
    `PytestUnknownMarkWarning`.
    """
    config.addinivalue_line(
        "markers",
        "live: hits the real LLM API; skipped by default. Requires ANTHROPIC_API_KEY.",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register `--run-slow` so the N=10000 size test can be opted into.

    The transparency-log N=10000 case is intentionally hidden behind
    this flag because the atomic temp+replace append is O(N) per
    call — extrapolated total runtime is ~70 minutes on a typical
    laptop. Operators refresh the size number in
    `docs/performance.md` on demand, not on every PR.
    """
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="Run slow benchmarks (e.g. transparency-log N=10000).",
    )


@pytest.fixture(autouse=True)
def _deterministic_rng() -> None:
    """Re-seed at the start of every benchmark so cross-test ordering
    cannot leak randomness from one benchmark into another."""
    random.seed(SEED)


@pytest.fixture(autouse=True)
def _isolate_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Sandbox `data/` writes (pins, transparency log) under tmp_path.

    Why `autouse`: benchmark code paths quietly touch `data_root()` when
    they verify Charters (`_check_pin` writes to `pins.json`); without
    isolation the dev machine's real `data/` accumulates noise.
    """
    monkeypatch.setenv("CHARTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHARTER_TRANSPARENCY_LOG", str(tmp_path / "transparency.log"))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_jwks_cache() -> Generator[None, None, None]:
    """Each benchmark starts with a cold JWKS cache so warm/cold
    comparisons stay honest."""
    keys_mod.clear_cache()
    yield
    keys_mod.clear_cache()


@pytest.fixture
def fake_grader_pass() -> FakeGrader:
    """Grader that always returns `matches_subset=true` with zero latency."""
    return FakeGrader()


@pytest.fixture
def stub_http_get(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `httpx.get` (the module-global, shared by `charter.keys`
    and `charter.mcp_server`) with a router from URL -> JSON dict.

    Tests populate `store[url] = payload_dict`. The router returns
    that dict for matches and raises 404 otherwise. This serves BOTH
    the Charter fetch path AND the JWKS lookup path (both call
    `httpx.get`) without ambiguity: each test publishes URLs for
    every endpoint it expects the SUT to hit.
    """
    store: dict[str, Any] = {}

    class _StubResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    def fake_get(url: str, **_kwargs: Any) -> _StubResponse:
        if url not in store:
            import httpx

            req = httpx.Request("GET", url)
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)
        return _StubResponse(store[url])

    # Patch the underlying module attribute. `charter.keys` and
    # `charter.mcp_server` share the same `httpx` module reference,
    # so one monkeypatch covers both call sites.
    monkeypatch.setattr("httpx.get", fake_get)
    return store
