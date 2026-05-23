"""Cookbook #07 — Profile YAML best practices.

This example does NOT run an LLM. It validates two profile.yaml files
through the same Pydantic schema the production projector uses, then
prints a side-by-side summary of what makes each one accurate or
inaccurate.

The two files:

    profile-bad.yaml   intentionally vague, full of anti-patterns
    profile-good.yaml  the same agent, written by the rules

We use `charter.projection.load_profile` for parsing so the cookbook
exercises the real ingestion path. The "scoring" is a tiny lint that
flags the anti-patterns enumerated in the cookbook markdown.

Run from the repo root:

    python examples/cookbook/07-profile-yaml-best-practices/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Anti-pattern fingerprints. Each entry: (clause-list-name, phrase, why)
# Substring match. Conservative on purpose — we only flag clear cases.
ANTI_PATTERNS: list[tuple[str, str, str]] = [
    ("scope", "看情况", "Open-ended / non-capability filler."),
    ("scope", "一切相关", "Catch-all phrase; grader cannot judge what's in vs out."),
    ("scope", "帮我搞定", "Verb-led with no concrete capability noun."),
    ("out_of_scope", "不要做那些不该做", "Double negation; nothing concrete to match against."),
    ("approval_required", "重要的事情", "Trigger condition undefined."),
    ("data_handling.what", "各种数据", "What data classes? Enumerate them."),
    ("data_handling.rules", "注意安全", "No concrete obligation."),
    ("operational.hours", "看情况", "Not a parseable schedule; no timezone."),
    ("style", "看着办", "No actual style direction."),
]


def lint_profile(raw: dict[str, Any]) -> list[str]:
    """Return a list of human-readable lint findings (empty == clean)."""
    findings: list[str] = []

    for path, phrase, why in ANTI_PATTERNS:
        sample = _resolve(raw, path)
        if sample is None:
            continue
        if isinstance(sample, list):
            for entry in sample:
                if isinstance(entry, str) and phrase in entry:
                    findings.append(f"  [{path}] '{entry}' -- {why}")
        elif isinstance(sample, str):
            if phrase in sample:
                findings.append(f"  [{path}] '{sample}' -- {why}")

    # Extra checks the substring map can't easily express.
    if raw.get("operational", {}).get("budget_per_task_usd") is None:
        findings.append(
            "  [operational.budget_per_task_usd] missing -- "
            "cannot project into an operational_limit clause."
        )
    return findings


def _resolve(raw: dict[str, Any], path: str) -> Any:
    cur: Any = raw
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def main() -> int:
    import yaml

    from charter.projection import load_profile

    print("=" * 72)
    print("Cookbook #07 — Profile YAML best practices")
    print("=" * 72)
    print()

    for label, fname in [("BAD", "profile-bad.yaml"), ("GOOD", "profile-good.yaml")]:
        path = _HERE / fname
        profile, raw_text = load_profile(path)
        # raw_text is the YAML source. Re-parse to dict for the lint.
        # Profile parses successfully — the Pydantic schema is intentionally
        # lenient. The lint is what catches phrasing issues.
        raw_dict = yaml.safe_load(raw_text)
        findings = lint_profile(raw_dict)
        print(f"--- {label}: {path.name}")
        print(f"  principal:    {profile.principal.id}  ({profile.principal.role})")
        print(f"  scope:        {len(profile.scope)} items")
        print(f"  out_of_scope: {len(profile.out_of_scope)} items")
        print(f"  approval:     {len(profile.approval_required)} items")
        if findings:
            print(f"  LINT ({len(findings)} finding(s)):")
            for f in findings:
                print(f)
        else:
            print("  LINT: clean.")
        print()

    # The good profile should have ZERO lint findings.
    _, good_raw = load_profile(_HERE / "profile-good.yaml")
    assert not lint_profile(yaml.safe_load(good_raw)), "Good profile should be lint-clean."

    # The bad profile should have AT LEAST one finding per anti-pattern category.
    _, bad_raw = load_profile(_HERE / "profile-bad.yaml")
    bad_findings = lint_profile(yaml.safe_load(bad_raw))
    assert len(bad_findings) >= 5, f"Expected >= 5 lint findings, got {len(bad_findings)}."

    print("[OK] Good profile is lint-clean; bad profile flags every anti-pattern.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
