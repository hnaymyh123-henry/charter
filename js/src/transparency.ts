/**
 * Transparency log chain verification (SPEC.md §8).
 *
 * The log is JSON-Lines; each entry is hash-chained via:
 *   - `prev_hash` = previous entry's `entry_hash` (genesis is
 *     `sha256:` + "0".repeat(64))
 *   - `entry_hash` = `sha256(canonical_json(entry without entry_hash))`
 *
 * Walking the log MUST verify both the prev_hash linkage AND that
 * every `entry_hash` matches a fresh hash of its own fields. Any
 * mismatch fails the chain.
 *
 * This module verifies AN ALREADY-FETCHED log (callers pass the
 * parsed entries). Log storage / persistence is out of scope for
 * `@charter/core` — it lives in `@charter/server` (deferred).
 */

import { createHash } from "node:crypto";

export const GENESIS_PREV_HASH = `sha256:${"0".repeat(64)}`;

/**
 * Shape of one transparency log entry on the wire (SPEC.md §8.1).
 * We don't define a strict zod schema here because the runner already
 * receives parsed JSON; if you want validation, wrap with zod yourself.
 */
export interface TransparencyEntry {
  seq: number;
  charter_id: string;
  binding: { principal_id: string; agent_id: string };
  issuer_kid: string | null;
  issuer_signature: string;
  appended_at: string;
  prev_hash: string;
  entry_hash: string;
}

export interface ChainVerification {
  ok: boolean;
  entries: number;
  head_hash: string;
  broken_at_seq?: number;
  reason?: string;
}

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeysDeep);
  }
  if (value !== null && typeof value === "object") {
    const src = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(src).sort()) {
      out[k] = sortKeysDeep(src[k]);
    }
    return out;
  }
  return value;
}

function canonicalJsonBytes(payload: unknown): Buffer {
  return Buffer.from(JSON.stringify(sortKeysDeep(payload)), "utf-8");
}

/**
 * Verify a transparency-log chain.
 *
 * Returns `{ ok: true, entries, head_hash }` on a clean walk, or
 * `{ ok: false, entries, head_hash, broken_at_seq, reason }` at the
 * first mismatch. An empty list is a degenerate-valid case (the
 * genesis state) and returns `ok: true`.
 */
export function verifyLogChain(
  entries: readonly TransparencyEntry[],
): ChainVerification {
  if (entries.length === 0) {
    return {
      ok: true,
      entries: 0,
      head_hash: GENESIS_PREV_HASH,
    };
  }

  let expectedPrev = GENESIS_PREV_HASH;
  for (const entry of entries) {
    if (entry.prev_hash !== expectedPrev) {
      return {
        ok: false,
        entries: entries.length,
        head_hash: entries[entries.length - 1]!.entry_hash,
        broken_at_seq: entry.seq,
        reason: `prev_hash mismatch at seq=${entry.seq}: expected ${expectedPrev}, found ${entry.prev_hash}`,
      };
    }

    // Recompute entry_hash from the entry sans entry_hash field.
    const withoutHash: Record<string, unknown> = { ...entry };
    delete withoutHash["entry_hash"];
    const digest = createHash("sha256").update(canonicalJsonBytes(withoutHash)).digest("hex");
    const recomputed = `sha256:${digest}`;
    if (recomputed !== entry.entry_hash) {
      return {
        ok: false,
        entries: entries.length,
        head_hash: entries[entries.length - 1]!.entry_hash,
        broken_at_seq: entry.seq,
        reason: `entry_hash mismatch at seq=${entry.seq}: recomputed ${recomputed}, found ${entry.entry_hash}`,
      };
    }
    expectedPrev = entry.entry_hash;
  }

  return {
    ok: true,
    entries: entries.length,
    head_hash: entries[entries.length - 1]!.entry_hash,
  };
}
