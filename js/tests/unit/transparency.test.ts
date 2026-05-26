/**
 * Transparency log chain verification tests (SPEC.md §8).
 *
 * Builds a small consistent log via the JS canonical-bytes helpers, then
 * asserts:
 *   - happy walk → ok=true, head_hash matches last entry_hash
 *   - tampering prev_hash, entry_hash, or any other field flips ok=false
 *     and reports the broken seq
 *   - empty list is a degenerate-valid genesis state
 */

import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";

import {
  GENESIS_PREV_HASH,
  verifyLogChain,
  type TransparencyEntry,
} from "../../src/transparency.js";

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
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

function buildEntry(
  seq: number,
  prevHash: string,
  binding: { principal_id: string; agent_id: string } = {
    principal_id: "alice@acme.com",
    agent_id: "research_agent",
  },
): TransparencyEntry {
  const partial = {
    seq,
    charter_id: `charter:alice@acme.com:research_agent:${seq}`,
    binding,
    issuer_kid: null,
    issuer_signature: "ed25519:" + "x".repeat(seq),
    appended_at: `2026-05-23T12:00:0${seq}Z`,
    prev_hash: prevHash,
  };
  const digest = createHash("sha256")
    .update(JSON.stringify(sortKeysDeep(partial)))
    .digest("hex");
  return { ...partial, entry_hash: `sha256:${digest}` };
}

function buildChain(n: number): TransparencyEntry[] {
  const entries: TransparencyEntry[] = [];
  let prev = GENESIS_PREV_HASH;
  for (let i = 1; i <= n; i++) {
    const e = buildEntry(i, prev);
    entries.push(e);
    prev = e.entry_hash;
  }
  return entries;
}

describe("verifyLogChain — happy paths", () => {
  it("returns ok=true and head_hash=genesis on empty input", () => {
    const r = verifyLogChain([]);
    expect(r.ok).toBe(true);
    expect(r.entries).toBe(0);
    expect(r.head_hash).toBe(GENESIS_PREV_HASH);
  });

  it("verifies a 1-entry chain", () => {
    const chain = buildChain(1);
    const r = verifyLogChain(chain);
    expect(r.ok).toBe(true);
    expect(r.entries).toBe(1);
    expect(r.head_hash).toBe(chain[0]!.entry_hash);
  });

  it("verifies a 5-entry chain", () => {
    const chain = buildChain(5);
    const r = verifyLogChain(chain);
    expect(r.ok).toBe(true);
    expect(r.entries).toBe(5);
    expect(r.head_hash).toBe(chain[4]!.entry_hash);
  });
});

describe("verifyLogChain — tampering", () => {
  it("detects a tampered prev_hash mid-chain", () => {
    const chain = buildChain(3);
    chain[1]!.prev_hash = "sha256:" + "0".repeat(64);
    const r = verifyLogChain(chain);
    expect(r.ok).toBe(false);
    expect(r.broken_at_seq).toBe(2);
    expect(r.reason).toMatch(/prev_hash mismatch/);
  });

  it("detects a tampered entry_hash", () => {
    const chain = buildChain(2);
    chain[0]!.entry_hash = "sha256:" + "f".repeat(64);
    const r = verifyLogChain(chain);
    expect(r.ok).toBe(false);
    expect(r.broken_at_seq).toBe(1);
    expect(r.reason).toMatch(/entry_hash mismatch/);
  });

  it("detects a tampered charter_id (entry_hash recomputation flags it)", () => {
    const chain = buildChain(2);
    // Mutate a signed field without recomputing entry_hash.
    chain[0]!.charter_id = "charter:evil:swap:1";
    const r = verifyLogChain(chain);
    expect(r.ok).toBe(false);
    expect(r.broken_at_seq).toBe(1);
  });

  it("detects a broken genesis prev_hash", () => {
    const chain = buildChain(1);
    chain[0]!.prev_hash = "sha256:" + "1".repeat(64);
    const r = verifyLogChain(chain);
    expect(r.ok).toBe(false);
    expect(r.broken_at_seq).toBe(1);
    expect(r.reason).toMatch(/prev_hash/);
  });
});
