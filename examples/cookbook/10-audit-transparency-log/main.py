"""Cookbook #10 — Audit the transparency log.

Walks a full audit end-to-end:

    1. Seed three Charters (two bindings under Alice, one under Bob).
       Each sign call appends to data/transparency.log.
    2. Invoke `charter audit verify` programmatically (calls into
       transparency.verify_chain) to walk every entry and check that
       prev_hash <- previous entry_hash, recomputed entry_hash matches
       stored, etc.
    3. Invoke `charter audit show <charter_id>` programmatically to
       print one Charter's transparency entry + every related entry
       (same principal_id).
    4. Tamper with one log line (flip one hex char in entry_hash) and
       re-verify: chain breaks at the tampered seq, audit returns
       broken=True with the row info.

The CLI surface lives in `charter/cli.py` (`charter audit verify` /
`charter audit show`). The example calls the same underlying
`charter.transparency` API so you can run it without `subprocess`.

Run from the repo root:

    python examples/cookbook/10-audit-transparency-log/main.py
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _seed_charter(principal_id: str, agent_id: str) -> str:
    from charter.schema import (
        AgentOperator,
        Binding,
        Charter,
        Clause,
        Issuer,
        Lifecycle,
        Principal,
        Provenance,
        Summary,
    )
    from charter.signing import public_key_to_string, sign_charter
    from charter.storage import ensure_issuer_key, save_charter

    now = datetime.now(UTC).replace(microsecond=0)
    pk = ensure_issuer_key(principal_id)
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary=f"Principal {principal_id}"),
        issuer=Issuer(id=principal_id, relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(plain_language=f"Test Charter for {agent_id}."),
        clauses=[Clause(id="C-001", type="scope", text=f"Work for {agent_id}")],
        lifecycle=Lifecycle(
            issued_at=now, valid_until=now + timedelta(days=30), status="active"
        ),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(pk.public_key()),
            issuer_signature="",
            generated_at=now,
        ),
    )
    sign_charter(charter, pk)
    save_charter(charter)
    return charter.charter_id


def main() -> int:
    scratch = _ROOT / "data" / "cookbook_10"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)

    print("=" * 72)
    print("Cookbook #10 — Audit the transparency log")
    print("=" * 72)
    print()

    # ---- Step 1: seed three Charters --------------------------------------
    alice_research_id = _seed_charter("alice@acme.com", "research_agent_v1")
    alice_billing_id = _seed_charter("alice@acme.com", "billing_agent_v1")
    bob_research_id = _seed_charter("bob@startup.io", "research_agent_v1")
    print("step 1  Seeded three Charters:")
    print(f"          {alice_research_id}")
    print(f"          {alice_billing_id}")
    print(f"          {bob_research_id}")
    print()

    from charter import transparency

    log_path = transparency.log_file_path()
    entries = list(transparency.read_log())
    print(f"        transparency.log at {log_path}")
    print(f"        entries: {len(entries)}  (one per signed Charter)")
    print()

    # ---- Step 2: charter audit verify (programmatically) ------------------
    result = transparency.verify_chain()
    print(f"step 2  charter audit verify --remote=(local)")
    print(f"          ok={result.ok}  entries={result.entries}")
    print(f"          head_hash={result.head_hash}")
    assert result.ok, f"verify failed: {result.reason}"
    print(f"        [OK] chain verified")
    print()

    # ---- Step 3: charter audit show <charter_id> --------------------------
    target = transparency.get_entry(alice_research_id)
    assert target is not None
    print(f"step 3  charter audit show {alice_research_id}")
    print(f"          seq:          {target.seq}")
    print(f"          principal_id: {target.binding['principal_id']}")
    print(f"          agent_id:     {target.binding['agent_id']}")
    print(f"          issuer_kid:   {target.issuer_kid}")
    print(f"          appended_at:  {target.appended_at.isoformat()}")
    print(f"          prev_hash:    {target.prev_hash}")
    print(f"          entry_hash:   {target.entry_hash}")
    related = [
        e
        for e in list(transparency.read_log())
        if e.charter_id != target.charter_id
        and e.binding["principal_id"] == target.binding["principal_id"]
    ]
    print(f"          related entries (same principal_id, different binding): {len(related)}")
    for e in related:
        marker = "* " if e.binding["agent_id"] == target.binding["agent_id"] else "  "
        print(f"            {marker}seq {e.seq:>3}  {e.charter_id}")
    assert len(related) == 1, f"Expected 1 related entry, got {len(related)}"
    print()

    # ---- Step 4: tamper + re-verify --------------------------------------
    raw_lines = log_path.read_text(encoding="utf-8").splitlines()
    # Flip one hex character in the entry_hash field of the FIRST entry.
    tampered_first = raw_lines[0].replace(
        f'"entry_hash": "{entries[0].entry_hash}"',
        f'"entry_hash": "{_flip_one_hex(entries[0].entry_hash)}"',
    )
    assert tampered_first != raw_lines[0], "tamper produced no diff -- check the field name"
    log_path.write_text("\n".join([tampered_first, *raw_lines[1:]]) + "\n", encoding="utf-8")
    print("step 4  Tampered with line 1 (flipped one hex char in entry_hash).")

    result_tampered = transparency.verify_chain()
    print(f"        re-run verify -> ok={result_tampered.ok}")
    print(f"          broken_at_seq: {result_tampered.broken_at_seq}")
    print(f"          reason:        {result_tampered.reason}")
    assert not result_tampered.ok, "Tampered chain should NOT verify"
    assert result_tampered.broken_at_seq == 1 or result_tampered.broken_at_seq == 2, (
        f"Expected break at seq 1 or 2 (depending on which check fires first), "
        f"got {result_tampered.broken_at_seq}"
    )
    print("        [OK] Tamper detected by chain verification.")
    print()

    print("[OK] charter audit verify caught the tamper; charter audit show worked end-to-end.")
    return 0


def _flip_one_hex(s: str) -> str:
    """Flip the FIRST hex char after the 'sha256:' prefix to break the
    chain deterministically. Substitutes a -> b, 0 -> 1, etc."""
    prefix, _, digest = s.partition(":")
    assert len(digest) == 64
    first = digest[0]
    flipped = "b" if first == "a" else ("a" if first == "0" else "0")
    return f"{prefix}:{flipped}{digest[1:]}"


if __name__ == "__main__":
    sys.exit(main())
