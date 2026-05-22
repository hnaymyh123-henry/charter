"""Cookbook #01 — Write a Charter for an Accountant.

End-to-end happy path with NO live LLM call required:

    1. Load profiles/alice.yaml (under this directory).
    2. Project + sign the Charter via scripts.seed_demo.seed_from_profile
       (which substitutes hand-curated clauses for the projector LLM
       call but runs the same sign / save code path).
    3. Pretty-print a summary of the resulting Charter from disk.

Run from the repo root:

    python examples/cookbook/01-write-charter-for-accountant/main.py

The script writes one Charter to data/charters/ and prints its
charter_id, binding, validity window, file path, and the first few
clauses. That is exactly what `charter inspect` would show you.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent  # examples/cookbook/01-.../main.py -> repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    # Import after sys.path is patched so this works from any cwd.
    from charter.storage import load_charter
    from scripts.seed_demo import seed_from_profile

    profile_path = _HERE / "profile.yaml"
    if not profile_path.exists():
        print(f"ERROR: profile.yaml not found at {profile_path}", file=sys.stderr)
        return 1

    print("=" * 72)
    print("Cookbook #01 — Write a Charter for an Accountant")
    print("=" * 72)
    print()

    # Step 1+2: project + sign + save. seed_from_profile prints its own
    # [1/3]..[3/3] progress lines.
    charter = seed_from_profile(profile_path)

    # Step 3: re-read from disk to prove the round-trip works, then
    # pretty-print a summary. This is the same thing `charter inspect`
    # would do.
    reloaded = load_charter(charter.binding.principal_id, charter.binding.agent_id)
    assert reloaded is not None, "Just-saved Charter not found on disk."

    print()
    print("--- inspect (re-read from disk) ---")
    print(f"charter_id: {reloaded.charter_id}")
    print(f"binding:    {reloaded.binding.principal_id} x {reloaded.binding.agent_id}")
    print(
        f"valid:      {reloaded.lifecycle.issued_at.date()} -> "
        f"{reloaded.lifecycle.valid_until.date()} ({reloaded.lifecycle.status})"
    )
    print(f"summary:    {reloaded.summary.plain_language}")
    print()
    print(f"clauses ({len(reloaded.clauses)}):")
    for c in reloaded.clauses:
        # Show first 80 chars of each clause text so output stays scannable.
        snippet = c.text if len(c.text) <= 80 else c.text[:77] + "..."
        print(f"  {c.id}  {c.type:18s}  {snippet}")
    print()
    print("[OK] Charter written, re-read, and inspected. Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
