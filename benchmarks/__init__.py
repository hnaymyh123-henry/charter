"""Performance baseline + benchmarks (Issue #43, B3.10).

Default `pytest` does NOT collect this directory — see `pyproject.toml`'s
`[tool.pytest.ini_options] testpaths = ["tests"]`. Run the benchmarks
explicitly with:

    pytest benchmarks/ --benchmark-only

See `docs/performance.md` for the 2026-05-23 baseline numbers, the test
hardware footprint, and the regression-review policy.
"""
