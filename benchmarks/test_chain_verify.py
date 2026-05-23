"""Benchmark `verify_chain` — strict (string-based) and semantic (LLM).

Strict mode is the default operator path. Semantic mode is opt-in for
chains where parent and child are semantically equivalent but not
string-equal (ADR-010). We chart both at multiple depths so the docs
table can show the cost trade-off explicitly.

For semantic, we use a fake grader that returns canned passing JSON
with 0 latency — the cost reflects the orchestration overhead, not
the LLM round-trip. `docs/performance.md` separately tabulates
"depth × per-call grader latency" so an operator can multiply by
their model's typical latency to predict end-to-end semantic verify
time.
"""

from __future__ import annotations

from typing import Any

import pytest

from charter.chain import verify_chain

from ._factories import FakeGrader, make_chain


@pytest.mark.parametrize("depth", [1, 3, 5, 10])
def test_bench_chain_verify_strict(benchmark: Any, depth: int) -> None:
    """Strict mode: text equality / containment, no LLM.

    Each link in the chain is verified by walking
    (out_of_scope + approval_required + scope) clause sets — all O(M^2)
    in clause count per pair but M is fixed at 3 by `make_chain`.
    """
    chain = make_chain(depth)
    # Strict mode walks consecutive (child, parent) pairs.
    pairs = [(chain[i][0], chain[i - 1][0]) for i in range(1, len(chain))]

    def run() -> None:
        for child, parent in pairs:
            assert verify_chain(child, parent, mode="strict") is True

    if depth == 1:
        # No pairs to verify — single-link "chain" is trivially valid.
        # Still benchmark the no-op so the row exists in the report.
        benchmark(lambda: None)
        return

    benchmark(run)


@pytest.mark.parametrize("depth", [1, 3, 5])
def test_bench_chain_verify_semantic(benchmark: Any, depth: int) -> None:
    """Semantic mode with fake grader (zero latency).

    The fake grader returns `matches_subset=true` for every call. The
    benchmark measures cache lookups (after the first call per
    (parent_id, issued_at) pair) and grader-orchestration overhead.
    """
    chain = make_chain(depth)
    pairs = [(chain[i][0], chain[i - 1][0]) for i in range(1, len(chain))]
    grader = FakeGrader()

    def run() -> None:
        for child, parent in pairs:
            assert (
                verify_chain(
                    child,
                    parent,
                    mode="semantic",
                    grader_client=grader,
                )
                is True
            )

    if depth == 1:
        benchmark(lambda: None)
        return

    benchmark(run)


@pytest.mark.parametrize("depth", [3, 5])
def test_bench_chain_verify_semantic_with_grader_latency_100ms(benchmark: Any, depth: int) -> None:
    """Semantic mode with 100ms-per-call grader (haiku-class).

    Charted here so operators can read off "5-hop chain verified
    semantically through a haiku-tier grader = N seconds" without
    hand-multiplying.
    """
    chain = make_chain(depth)
    pairs = [(chain[i][0], chain[i - 1][0]) for i in range(1, len(chain))]
    grader = FakeGrader(latency_s=0.1)

    def run() -> None:
        for child, parent in pairs:
            verify_chain(
                child,
                parent,
                mode="semantic",
                grader_client=grader,
            )

    benchmark.pedantic(run, rounds=1, iterations=1)
