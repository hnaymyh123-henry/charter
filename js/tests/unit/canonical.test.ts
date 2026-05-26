/**
 * Canonical-bytes tests.
 *
 * Pulls 4 cross-language vectors from `conformance/vectors/sign/` so a
 * regression in JS canonical bytes shows up as a hex digest mismatch
 * against the same SHA-256 the Python reference produces. The vectors
 * are the protocol's source of truth (SPEC.md §1).
 */

import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

import { canonicalBytes, canonicalBytesSha256 } from "../../src/canonical.js";
import { minimalCharter } from "./fixtures.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const VECTORS_ROOT = resolve(__dirname, "..", "..", "..", "conformance", "vectors", "sign");

function loadVector(name: string): any {
  return JSON.parse(readFileSync(resolve(VECTORS_ROOT, name), "utf-8"));
}

function sha256Hex(bytes: Uint8Array): string {
  return "sha256:" + createHash("sha256").update(bytes).digest("hex");
}

describe("canonicalBytes — cross-language vectors", () => {
  it("matches the minimal Charter vector byte-for-byte", () => {
    const v = loadVector("canonical_bytes_minimal.json");
    const out = canonicalBytes(v.input.charter);
    expect(out.length).toBe(v.expected_output.canonical_bytes_length);
    expect(sha256Hex(out)).toBe(v.expected_output.canonical_bytes_sha256);
  });

  it("elides private_fields=null entries from clauses (substring check)", () => {
    const v = loadVector("canonical_bytes_elides_private_fields_none.json");
    const out = canonicalBytes(v.input.charter);
    const text = new TextDecoder().decode(out);
    // Vector expects the substring NOT to appear in canonical bytes.
    expect(text.includes(v.input.substring)).toBe(v.expected_output.contains_substring);
  });

  it("clears transparency_log_id from the signed payload (pre/post equality)", () => {
    const v = loadVector("canonical_bytes_excludes_log_id.json");
    const pre = v.input.charter_pre_assign;
    const post = JSON.parse(JSON.stringify(pre));
    post.provenance.transparency_log_id = v.input.charter_post_assign_log_id;

    const preBytes = canonicalBytes(pre);
    const postBytes = canonicalBytes(post);
    expect(sha256Hex(preBytes)).toBe(v.expected_output.pre_sha256);
    expect(sha256Hex(postBytes)).toBe(v.expected_output.post_sha256);
    // Confirms invariant: assigning a transparency_log_id MUST NOT change
    // canonical bytes (it's stripped before signing).
    expect(sha256Hex(preBytes)).toBe(sha256Hex(postBytes));
  });

  it("produces different bytes when a clause text changes (collision sanity)", () => {
    const v = loadVector("canonical_bytes_clause_edit_changes_hash.json");
    const a = canonicalBytes(v.input.charter_a);
    const b = canonicalBytes(v.input.charter_b);
    expect(sha256Hex(a)).toBe(v.expected_output.sha256_a);
    expect(sha256Hex(b)).toBe(v.expected_output.sha256_b);
    expect(sha256Hex(a)).not.toBe(sha256Hex(b));
  });
});

describe("canonicalBytes — local properties", () => {
  it("never mutates its input", () => {
    const c = minimalCharter();
    c.provenance.issuer_signature = "ed25519:should_be_cleared_in_bytes_only";
    c.provenance.transparency_log_id = 42;
    const snapshot = JSON.stringify(c);
    canonicalBytes(c);
    expect(JSON.stringify(c)).toBe(snapshot);
  });

  it("clears issuer_signature in the produced bytes", () => {
    const c = minimalCharter();
    c.provenance.issuer_signature = "ed25519:should_not_survive";
    const bytes = canonicalBytes(c);
    const text = new TextDecoder().decode(bytes);
    // After clearing, the empty-string value lands at the sorted position.
    expect(text).toContain('"issuer_signature":""');
    expect(text).not.toContain("should_not_survive");
  });

  it("clears transparency_log_id in the produced bytes", () => {
    const c = minimalCharter();
    c.provenance.transparency_log_id = 7;
    const bytes = canonicalBytes(c);
    const text = new TextDecoder().decode(bytes);
    expect(text).toContain('"transparency_log_id":null');
    expect(text).not.toContain('"transparency_log_id":7');
  });

  it("elides private_fields=null at the key level (not as null)", () => {
    const c = minimalCharter();
    c.clauses[0]!.private_fields = null;
    const text = new TextDecoder().decode(canonicalBytes(c));
    // The entire key is absent — NOT "private_fields":null.
    expect(text).not.toContain('"private_fields":null');
    expect(text).not.toContain('"private_fields":');
  });

  it("keeps private_fields when populated (ADR-011 path 1)", () => {
    const c = minimalCharter();
    c.clauses[0]!.private_fields = [
      {
        span_start: 0,
        span_end: 18,
        disclosure_hash: "sha256:" + "a".repeat(64),
      },
    ];
    const text = new TextDecoder().decode(canonicalBytes(c));
    expect(text).toContain('"private_fields"');
    expect(text).toContain('"disclosure_hash":"sha256:' + "a".repeat(64) + '"');
  });

  it("is order-independent over input key insertion order", () => {
    const c1 = minimalCharter();
    // Build c2 by re-inserting top-level keys in a different order. The
    // canonical bytes MUST be identical.
    const c2 = JSON.parse(JSON.stringify(c1));
    const reordered: Record<string, unknown> = {};
    for (const k of [...Object.keys(c2)].reverse()) {
      reordered[k] = c2[k];
    }
    const b1 = canonicalBytes(c1);
    const b2 = canonicalBytes(reordered as unknown as typeof c1);
    expect(sha256Hex(b1)).toBe(sha256Hex(b2));
  });

  it("canonicalBytesSha256 matches recomputing manually", async () => {
    const c = minimalCharter();
    const expected = sha256Hex(canonicalBytes(c));
    const helper = await canonicalBytesSha256(c);
    expect(helper).toBe(expected);
  });
});
