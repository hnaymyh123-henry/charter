"""Benchmark transparency-log growth & verification.

What we care about:
    Per-append cost is O(N) today (atomic temp+replace rewrites the
    whole file). The N=10000 numbers in `docs/performance.md` are the
    operator's reality check: at this growth rate, when do they need
    to roll the log into a separate file or move to an append-with-fsync
    implementation?

`verify_chain` cost is linear in N; the bench captures the constant
so doubling N predicts the new wall-clock cost.

Implementation note: building one signed Charter and cloning it
per-append keeps the loop dominated by the *append* path (the thing
we want to measure), not by Ed25519 keygen + signing (a one-time
cost in real life). `transparency.append` only reads metadata
fields, not the signature itself, so cloning is faithful to what
the path costs an operator at steady state.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from charter import transparency
from charter.schema import Charter

from ._factories import make_signed_charter


def _template() -> Charter:
    """Build one signed Charter that the populate loop will clone."""
    charter, _ = make_signed_charter(charter_id="charter:bench:template:2026-05-23")
    return charter


def _populate_log(n: int, template: Charter | None = None) -> None:
    """Append N synthetic entries by cloning a template Charter.

    Each clone gets a distinct `charter_id` (so `append`'s idempotency
    short-circuit doesn't hide the write path) and `agent_id`.
    `transparency.append` only reads identifier/signature fields, so
    we don't need to re-sign each clone.
    """
    tpl = template if template is not None else _template()
    # Reset the log fully so test reps don't carry over the template
    # entry that `_template()` already appended via `sign_charter`.
    log_path = transparency.log_file_path()
    if log_path.exists():
        log_path.unlink()

    for i in range(n):
        clone = deepcopy(tpl)
        clone.charter_id = f"charter:bench:{i:06d}:2026-05-23"
        clone.binding.agent_id = f"agent_{i:06d}"
        transparency.append(clone)


@pytest.mark.parametrize("n", [100, 1000])
def test_bench_transparency_append_growth(benchmark: Any, n: int) -> None:
    """Append N Charters. The benchmarked function is the *entire*
    batch so pytest-benchmark's `mean` reports per-N batch cost; divide
    by N for per-append.

    N=10000 is exercised in the file-size measurement below — running
    it through pytest-benchmark's auto-tuner would burn minutes
    rebuilding the same 3MB file repeatedly. The single-call sized
    snapshot captures the asymptotic file size operators actually
    need to plan for.
    """
    tpl = _template()

    def run() -> None:
        _populate_log(n, template=tpl)

    # `pedantic` keeps the round count tight — N=1000 is already a
    # 500k-write workload per round; autotuning would explode this.
    # N=1000 takes ~40s/round; cap at 1 round to keep CI under 3 min.
    rounds = 3 if n <= 100 else 1
    benchmark.pedantic(run, rounds=rounds, iterations=1)


@pytest.mark.skipif(
    "not config.getoption('--run-slow', default=False)",
    reason=(
        "N=10000 transparency-log populate takes ~70 minutes on a"
        " typical laptop because the atomic temp+replace append is"
        " O(N) per call (O(N²) total). Opt in with"
        " `--run-slow` when refreshing the docs/performance.md table."
    ),
)
def test_transparency_log_size_at_n_10000(tmp_path: Path) -> None:
    """Snapshot file size at N=10000 — recorded in docs/performance.md.

    Not a `benchmark()` target: we want the byte count, not a timed
    loop. Run on demand:

        pytest benchmarks/test_transparency_growth.py::test_transparency_log_size_at_n_10000 \\
            --run-slow -s
    """
    _populate_log(10_000)
    log_path = transparency.log_file_path()
    assert log_path.exists()
    size_bytes = log_path.stat().st_size
    # Loose assertion — the absolute number is what operators care
    # about (and what `docs/performance.md` records); the bound below
    # is only to flag a runaway 10x regression.
    assert size_bytes > 0
    assert size_bytes < 50 * 1024 * 1024  # 50 MB ceiling
    # Echo the byte count so a `-s` run captures it for the docs table.
    print(f"\n[transparency-log] N=10000 file size = {size_bytes:,} bytes")


@pytest.mark.parametrize("n", [100, 1000])
def test_bench_transparency_verify_chain(benchmark: Any, n: int) -> None:
    """`verify_chain` walks the log and recomputes every hash.

    We populate ONCE outside the benchmark loop (verification is a
    read-only path, so per-iteration reset isn't required).
    """
    _populate_log(n)

    def run() -> None:
        result = transparency.verify_chain()
        assert result.ok, result.reason

    benchmark(run)
