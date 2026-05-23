"""Wrap the conformance runner in a pytest test so local `pytest` runs
the language-neutral vector suite alongside the rest of the test suite.

The CI workflow ALSO runs the runner as a standalone step (so the
runner's `--junit-xml` / `--filter` / non-zero-exit behavior is
exercised in CI without going through pytest). This test is the
"local developer's safety net" — `pytest -x -q` from the repo root
catches any vector regression before the developer pushes.

We invoke the runner as a subprocess (not via importlib) because the
runner module uses dataclasses + dynamic sys.path manipulation, which
can interact badly with importlib's spec-loading machinery on some
Python configurations. The subprocess approach also more faithfully
mirrors what CI actually runs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER = _REPO_ROOT / "conformance" / "runners" / "python" / "run.py"
_VECTORS = _REPO_ROOT / "conformance" / "vectors"


def test_conformance_runner_exists() -> None:
    """The runner must be on disk where CI + docs expect it."""
    assert _RUNNER.is_file(), f"conformance runner missing at {_RUNNER}"
    assert _VECTORS.is_dir(), f"conformance vectors dir missing at {_VECTORS}"


def test_all_conformance_vectors_pass() -> None:
    """Run the Python conformance runner across every vector via subprocess."""
    result = subprocess.run(
        [sys.executable, str(_RUNNER), "--vectors-dir", str(_VECTORS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"conformance runner exit_code={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    # Sanity-check the summary line is on stdout.
    assert "vectors passed" in result.stdout


def test_filter_flag_narrows_vector_set() -> None:
    """`--filter aggregate` should run ONLY aggregate vectors."""
    result = subprocess.run(
        [
            sys.executable,
            str(_RUNNER),
            "--vectors-dir",
            str(_VECTORS),
            "--filter",
            "aggregate",
            "-v",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"filtered runner exit_code={result.returncode}\nstdout:\n{result.stdout}"
    )
    pass_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("[PASS]")]
    assert pass_lines, "filter should have produced at least one PASS line"
    assert all("aggregate" in ln for ln in pass_lines), (
        f"filter leaked non-aggregate vectors:\n{result.stdout}"
    )


def test_unknown_vectors_dir_exits_nonzero(tmp_path: Path) -> None:
    """When the vectors dir is empty / missing, exit code should be non-zero."""
    empty = tmp_path / "empty_vectors"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, str(_RUNNER), "--vectors-dir", str(empty)],
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 2 when no vectors found at all (distinct from "vectors ran but
    # something failed", which is exit 1).
    assert result.returncode == 2, (
        f"expected exit 2 for empty dir, got {result.returncode}\n{result.stderr}"
    )
