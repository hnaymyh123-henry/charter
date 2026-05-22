# 10 — Audit the transparency log

> **TL;DR.** Every signed Charter appends one line to
> `data/transparency.log`, SHA-256-chained to the previous entry.
> `charter audit verify` walks the chain and exit-0's on success;
> `charter audit show <charter_id>` pretty-prints one entry plus every
> related entry from the same issuer. The cookbook example seeds three
> Charters, verifies, shows, then tampers with one log line and proves
> the audit catches the tamper at the exact `seq`.

## Why this guide

The transparency log answers the question:

> "What else has this issuer signed, and can I prove nothing was
> retroactively edited?"

Concretely:

- A calling agent fetches a Charter today. Two weeks later, they want to
  prove "the Charter I saw on day X was identical to the Charter I see
  now." The log entry from day X has the SHA-256 chain to the genesis
  hash; if the file was edited in place, the chain breaks.
- An auditor or a regulator wants the full issuance history for one
  principal — every Charter alice@acme.com ever signed. `charter audit
  show` gives them that, with full chain context.

This guide walks both commands end-to-end, and proves the tamper-
detection works.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- A few signed Charters on disk (or run the cookbook example which
  seeds three).

## Step-by-step

### 1. Sign some Charters

Every `sign_charter` call appends one entry to the log automatically.
You don't have to opt in:

```bash
python examples/cookbook/01-write-charter-for-accountant/main.py
python examples/cookbook/02-chain-with-budget/main.py
```

…or anything else that calls `charter.signing.sign_charter`. After
either of those, `data/transparency.log` (or
`$CHARTER_DATA_DIR/transparency.log` if overridden) is non-empty.

The cookbook example seeds three Charters under two different
principals so the audit has interesting content to walk.

### 2. `charter audit verify`

```bash
charter audit verify
# [OK] Transparency log verified
#   source:    data/transparency.log (local)
#   entries:   3
#   range:     seq 1 -> seq 3
#   head_hash: sha256:<64-hex>
```

What this checks per entry:

1. The entry's `prev_hash` equals the previous entry's `entry_hash`.
   For the first entry, `prev_hash` is the genesis (64 zeros).
2. Recomputing the entry's hash from its other fields yields the
   stored `entry_hash`.

Either check failing returns a chain-broken error with the specific
`seq` and the reason.

For a remote audit, point at any `charter-server`:

```bash
charter audit verify --remote https://charter.example.com
```

The CLI walks `/transparency/log` (NDJSON streaming endpoint) and
checks the chain on the way through.

For resumable audits, `--since N` skips entries with `seq <= N`. The
first checked entry's `prev_hash` is trusted as the anchor for that
slice (a separate `--since 0` run would catch any tampering of the
unseen prefix).

### 3. `charter audit show <charter_id>`

```bash
charter audit show charter:alice@acme.com:research_agent_v1:2026-05-22
#
# Charter: charter:alice@acme.com:research_agent_v1:2026-05-22
#   seq:              1
#   principal_id:     alice@acme.com
#   agent_id:         research_agent_v1
#   issuer_kid:       <16-hex>
#   appended_at:      2026-05-22T05:48:49+00:00
#   prev_hash:        sha256:0000...
#   entry_hash:       sha256:c886...
#
# Other entries from alice@acme.com (1):
#     seq    2  charter:alice@acme.com:billing_agent_v1:2026-05-22  (billing_agent_v1, 2026-05-22)
#
#   ('*' marks entries that share this exact binding.)
```

"Related" means *same issuer (principal_id)*. The `*` marker calls out
entries that also share the exact binding — that's how you spot
`renew` history (two entries for `(alice, research_agent_v1)` on
consecutive dates means a renewal happened).

### 4. Tamper detection

Flip one hex character in `entry_hash` on any log line and re-verify:

```bash
charter audit verify
# [ERROR] Chain broken at seq 1
#   reason: entry_hash mismatch at seq=1: recomputed sha256:<R>, found sha256:<T>
```

The CLI exits with code 1; downstream automation can rely on the
non-zero return.

The example does this by computing one tampered line and writing it
back over the first log line. The chain breaks on the very next
`verify` because the recomputed hash differs from the stored one.

## Verification

Run:

```bash
python examples/cookbook/10-audit-transparency-log/main.py
```

Expected output (hashes and timestamps will differ):

```
========================================================================
Cookbook #10 -- Audit the transparency log
========================================================================

step 1  Seeded three Charters:
          charter:alice@acme.com:research_agent_v1:<DATE>
          charter:alice@acme.com:billing_agent_v1:<DATE>
          charter:bob@startup.io:research_agent_v1:<DATE>

        transparency.log at <CHARTER_DATA_DIR>/transparency.log
        entries: 3  (one per signed Charter)

step 2  charter audit verify --remote=(local)
          ok=True  entries=3
          head_hash=sha256:<64-hex>
        [OK] chain verified

step 3  charter audit show charter:alice@acme.com:research_agent_v1:<DATE>
          seq:          1
          principal_id: alice@acme.com
          agent_id:     research_agent_v1
          issuer_kid:   <16-hex>
          appended_at:  <ISO>
          prev_hash:    sha256:0000...
          entry_hash:   sha256:<64-hex>
          related entries (same principal_id, different binding): 1
              seq   2  charter:alice@acme.com:billing_agent_v1:<DATE>

step 4  Tampered with line 1 (flipped one hex char in entry_hash).
        re-run verify -> ok=False
          broken_at_seq: 1
          reason:        entry_hash mismatch at seq=1: recomputed sha256:<R>, found sha256:<T>
        [OK] Tamper detected by chain verification.

[OK] charter audit verify caught the tamper; charter audit show worked end-to-end.
```

Asserts:

1. After step 1 the log file exists and has 3 entries.
2. Step 2's `verify_chain()` returns `ok=True`.
3. Step 3 finds the entry by `charter_id` and lists exactly 1 related
   entry (the second Alice binding).
4. Step 4's `verify_chain()` returns `ok=False` with
   `broken_at_seq=1` (the line we tampered with).

To run the CLI form against the same data:

```bash
CHARTER_DATA_DIR=data/cookbook_10 python -m charter.cli audit verify
```

…will print `[ERROR] Chain broken at seq 1` (because step 4 left the
file in a tampered state).

## Common pitfalls

- **Editing the log "to clean it up."** Don't. Any edit breaks the
  chain. To remove a stale entry you have to rotate the whole log
  (i.e. start over with a new genesis). The append-only contract is
  what makes the log auditable.
- **Confusing log entries with Charter content.** The log stores
  *identifiers* (charter_id, binding, kid, signature) plus chain
  metadata. It does NOT store clauses, summary, or principal role
  text. To re-fetch a Charter's body, hit `/<principal>/<agent>` on
  the issuer's server (or the on-disk file).
- **Expecting `audit verify` to verify Charter signatures.** It does
  not — that's what `_fetch_and_verify` does on each fetch. `audit
  verify` only walks the chain integrity. The two checks are
  complementary, not interchangeable.
- **Forgetting `--since` semantics.** `--since 0` is a full audit.
  `--since N` (N > 0) trusts the first checked entry's `prev_hash` as
  an anchor — fine for incremental polling, but the trust window is
  "anything before seq N+1." Run `--since 0` periodically to catch
  tampering in the historical prefix.
- **Sharing the log file across multiple issuers' machines.** The log
  is per-issuer-server, not per-principal-across-the-internet. Two
  separate `charter-server` instances each maintain their own log.
  Comparing entries across logs is meaningful only for the bits both
  observed (signed Charters they each saw).

## Related guides

- [01 — Write a Charter for an accountant](01-write-charter-for-accountant.md)
  — every Charter issued there shows up in the transparency log.
- [03 — Revoke without breaking in-flight](03-revoke-without-breaking-inflight.md)
  — revocations re-sign and append a new log entry, so the revocation
  timestamp is cryptographically attested.
- [09 — Deploy a JWKS and rotate keys](09-deploy-jwks.md) — key
  rotations show up as a new `issuer_kid` in the log entry, providing
  the rotation audit trail.

## What's next

- The transparency module is at
  [`charter/transparency.py`](../../charter/transparency.py); CLI surface
  is the `audit` group in [`charter/cli.py`](../../charter/cli.py).
- HTTP endpoints for remote audits are in
  [`charter/server.py`](../../charter/server.py):
  `/transparency/head`, `/transparency/log` (NDJSON stream),
  `/transparency/proof/<charter_id>` (linear-chain inclusion proof).
- Merkle-tree proofs with O(log n) size are on the v0.9+ backlog.
  Until then, every audit is a full linear walk — fine for tens of
  thousands of entries, less fine at internet scale. If you need
  truly scalable transparency, the `merkle_proof` field on
  `TransparencyEntry` is reserved (currently `None`) for future use.
