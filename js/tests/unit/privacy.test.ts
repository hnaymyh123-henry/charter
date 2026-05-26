/**
 * SD-JWT path 1 privacy tests (SPEC.md §10, ADR-011 path 1).
 *
 * redactClause: span-based redaction producing redacted text + private
 * fields + disclosures. Salt determinism (via test-only salt). Reject
 * overlapping or out-of-range spans.
 *
 * verifyDisclosure: confirms sha256(salt || value) matches the embedded
 * hash. Tampering with value, salt, or hash flips the result.
 *
 * matchRedacted: candidate equality check pinned to redacted clauses.
 */

import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";

import {
  redactClause,
  verifyDisclosure,
  matchRedacted,
  HASH_PREFIX_HEX_CHARS,
} from "../../src/privacy.js";

const FIXED_SALT = new Uint8Array(16).map((_, i) => i + 1);

function manualHash(salt: Uint8Array, value: string): string {
  const buf = Buffer.concat([Buffer.from(salt), Buffer.from(value, "utf-8")]);
  return "sha256:" + createHash("sha256").update(buf).digest("hex");
}

describe("redactClause", () => {
  it("returns the original text when no spans are supplied", () => {
    const r = redactClause("hello world", [], FIXED_SALT);
    expect(r.redacted_text).toBe("hello world");
    expect(r.private_fields).toEqual([]);
    expect(r.disclosures).toEqual([]);
  });

  it("redacts a single span and emits one disclosure", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT); // "Bob"
    expect(r.private_fields).toHaveLength(1);
    expect(r.disclosures).toHaveLength(1);
    expect(r.disclosures[0]!.span_value).toBe("Bob");
    // Placeholder format `[REDACTED:<8-hex>]`
    expect(r.redacted_text).toMatch(/^Pay \$500 to \[REDACTED:[0-9a-f]{8}\]\.$/);
  });

  it("redacted span placeholder points into redacted_text not source", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    const pf = r.private_fields[0]!;
    const slice = r.redacted_text.slice(pf.span_start, pf.span_end);
    expect(slice).toMatch(/^\[REDACTED:[0-9a-f]{8}\]$/);
  });

  it("redacts two non-overlapping spans (sorted by start)", () => {
    const text = "Pay $500 to Bob from Alice.";
    const r = redactClause(text, [
      [21, 26], // Alice (second)
      [12, 15], // Bob (first)
    ], FIXED_SALT);
    expect(r.disclosures).toHaveLength(2);
    // The order of disclosures follows sorted order — Bob first, Alice second.
    expect(r.disclosures[0]!.span_value).toBe("Bob");
    expect(r.disclosures[1]!.span_value).toBe("Alice");
  });

  it("throws on overlapping spans (ADR-011 path 1 invariant)", () => {
    expect(() =>
      redactClause("abcdef", [
        [0, 4],
        [3, 6],
      ], FIXED_SALT),
    ).toThrow(/overlapping/);
  });

  it("throws on out-of-range spans", () => {
    expect(() => redactClause("short", [[0, 10]], FIXED_SALT)).toThrow(
      /invalid span/,
    );
    expect(() => redactClause("short", [[-1, 2]], FIXED_SALT)).toThrow(
      /invalid span/,
    );
    expect(() => redactClause("short", [[3, 3]], FIXED_SALT)).toThrow(
      /invalid span/,
    );
  });

  it("disclosure_hash matches sha256(salt || value)", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    const expected = manualHash(FIXED_SALT, "Bob");
    expect(r.disclosures[0]!.disclosure_hash).toBe(expected);
    expect(r.private_fields[0]!.disclosure_hash).toBe(expected);
  });

  it("disclosure_id is the first 8 hex chars of the digest (no salt prefix)", () => {
    const r = redactClause("foo bar baz", [[4, 7]], FIXED_SALT);
    // The id derivation hashes (salt || value) but takes only the prefix.
    expect(r.disclosures[0]!.disclosure_id).toMatch(
      new RegExp(`^[0-9a-f]{${HASH_PREFIX_HEX_CHARS}}$`),
    );
  });

  it("disambiguates duplicate disclosure_ids with a numeric suffix", () => {
    // Two spans with the SAME plaintext + SAME salt collide on the
    // id-prefix. The second one MUST get a suffix instead of overwriting.
    const text = "foo bar foo";
    const r = redactClause(text, [
      [0, 3],
      [8, 11],
    ], FIXED_SALT);
    expect(r.disclosures[0]!.disclosure_id).not.toBe(r.disclosures[1]!.disclosure_id);
    expect(r.disclosures[1]!.disclosure_id).toMatch(/-1$/);
  });
});

describe("verifyDisclosure", () => {
  it("returns true on a valid disclosure", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    const d = r.disclosures[0]!;
    expect(verifyDisclosure(d, d.disclosure_hash)).toBe(true);
  });

  it("returns false when span_value is tampered", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    const d = r.disclosures[0]!;
    expect(verifyDisclosure({ ...d, span_value: "Eve" }, d.disclosure_hash)).toBe(
      false,
    );
  });

  it("returns false when salt is tampered", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    const d = r.disclosures[0]!;
    const tampered = { ...d, salt_hex: "00".repeat(16) };
    expect(verifyDisclosure(tampered, d.disclosure_hash)).toBe(false);
  });

  it("returns false when claimedHash != recomputed", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    const d = r.disclosures[0]!;
    expect(verifyDisclosure(d, "sha256:" + "0".repeat(64))).toBe(false);
  });

  it("returns false on malformed salt_hex (odd length)", () => {
    const d = {
      disclosure_id: "x",
      span_value: "Bob",
      salt_hex: "abc", // odd length
      disclosure_hash: "sha256:" + "0".repeat(64),
    };
    expect(verifyDisclosure(d, d.disclosure_hash)).toBe(false);
  });
});

describe("matchRedacted", () => {
  it("returns true when candidate is the plaintext of a present placeholder", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    expect(matchRedacted(r.redacted_text, "Bob", r.disclosures)).toBe(true);
  });

  it("returns false when candidate is a wrong plaintext", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    expect(matchRedacted(r.redacted_text, "Alice", r.disclosures)).toBe(false);
  });

  it("ignores disclosures whose placeholder is not in clauseText", () => {
    const text = "Pay $500 to Bob.";
    const r = redactClause(text, [[12, 15]], FIXED_SALT);
    // Probe with the unrelated clause text — the placeholder won't be present.
    expect(matchRedacted("a completely different clause", "Bob", r.disclosures)).toBe(
      false,
    );
  });
});
