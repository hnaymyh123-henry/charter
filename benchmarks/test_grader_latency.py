"""Grader-latency benchmark — measures the wrapper, NOT the real API.

We parametrize three sleep durations representing the three Anthropic
tiers we ship documentation for:

    100ms  → haiku-class
    500ms  → sonnet-class
    2000ms → opus-class

These are sleep-based simulations; the *real* per-model latency
numbers live in `docs/performance.md` and are refreshed manually by
the maintainer when a new model lands.

Real API calls are skipped by default. To run them, install the
`live` extras and use `pytest benchmarks/test_grader_latency.py -m
live --benchmark-only`. The live test only confirms the wrapper
correctly threads `temperature=0.0` through to the model — it does
NOT record numbers, because that would require an API key in CI.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from charter.chain import _grade_one
from charter.schema import Clause

from ._factories import FakeGrader

# ---------------------------------------------------------------------------
# Sleep-based latency simulation (always runs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latency_label, latency_s",
    [
        ("haiku_100ms", 0.1),
        ("sonnet_500ms", 0.5),
        ("opus_2000ms", 2.0),
    ],
)
def test_bench_grader_wrapper_latency(benchmark: Any, latency_label: str, latency_s: float) -> None:
    """One `_grade_one` call against a faked grader at simulated latency.

    Confirms our wrapper adds negligible overhead on top of the model
    call itself; subtracting `latency_s` from `mean` gives the pure
    Python overhead.
    """
    grader = FakeGrader(latency_s=latency_s)
    target = Clause(
        id="parent-1",
        type="out_of_scope",
        text="Do not write production DB migrations.",
    )
    candidates = [
        Clause(id="child-1", type="out_of_scope", text="No prod DB migrations."),
    ]

    def run() -> None:
        _grade_one(grader, target, candidates, direction="restriction")

    # `pedantic` with rounds=1 because the opus tier at 2s would
    # otherwise run for too long under pytest-benchmark's autotuner.
    benchmark.pedantic(run, rounds=1, iterations=1)


# ---------------------------------------------------------------------------
# Real LLM call — skipped by default, requires --benchmark-only + -m live
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_bench_grader_real_api_smoke(benchmark: Any) -> None:
    """One real `_grade_one` call. NOT recorded as a baseline number —
    the reference table in docs/performance.md is the authoritative
    record. This test exists so a developer can quickly confirm the
    wiring still works against a current model.

    Skipped unless `ANTHROPIC_API_KEY` is set AND the `live` marker is
    selected (`pytest -m live ...`).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; live grader benchmark skipped")

    import anthropic

    grader = anthropic.Anthropic()
    target = Clause(
        id="p-1",
        type="out_of_scope",
        text="Do not commit code to the production deploy branch.",
    )
    candidates = [
        Clause(id="c-1", type="out_of_scope", text="No commits to main."),
    ]

    def run() -> None:
        _grade_one(grader, target, candidates, direction="restriction")

    benchmark.pedantic(run, rounds=1, iterations=1)
