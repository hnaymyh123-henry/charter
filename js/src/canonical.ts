/**
 * Canonical bytes — the byte sequence that gets signed.
 *
 * SPEC.md §1 spells out the exact transformations:
 *
 *   1. `provenance.issuer_signature` is cleared to `""` (avoids the
 *      chicken-and-egg of signing a payload that contains its own
 *      signature).
 *   2. `provenance.transparency_log_id` is cleared to `null` (the log
 *      entry can only be written AFTER the signature is final, so the
 *      field is structurally outside the signed payload).
 *   3. For each clause whose `private_fields` is `null` / undefined, the
 *      ENTIRE `private_fields` key is removed (ADR-011 backward compat:
 *      Charters issued before the field existed must still verify).
 *   4. Object keys are sorted lexicographically at every nesting level.
 *   5. JSON is emitted with no whitespace between tokens.
 *
 * Cross-language byte-equivalence with Python's
 * `json.dumps(sort_keys=True, separators=(",", ":"))` is the WHOLE point.
 * Differences in Unicode escaping or float formatting would silently
 * break signatures across languages — the conformance vectors at
 * `vectors/sign/canonical_bytes_*.json` are the regression net.
 */

import type { Charter } from "./schema.js";

/**
 * Recursively sort the keys of plain objects (lexicographic) so
 * `JSON.stringify` produces a stable byte sequence regardless of input
 * insertion order. Arrays preserve order; null and primitives pass
 * through; nested objects/arrays are sorted depth-first.
 *
 * Behavior MUST match Python `json.dumps(..., sort_keys=True)` which
 * sorts at every nesting level by the JavaScript-equivalent
 * lexicographic order of UTF-16 code units. Since the Charter schema
 * uses ASCII-only keys, lexicographic and code-unit orderings agree;
 * if non-ASCII keys are introduced, this function still produces
 * stable output but the Python equivalent's behavior depends on
 * Python's default string comparison (which is by Unicode code point).
 */
function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortKeysDeep);
  }
  if (value !== null && typeof value === "object") {
    const src = value as Record<string, unknown>;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(src).sort()) {
      sorted[key] = sortKeysDeep(src[key]);
    }
    return sorted;
  }
  return value;
}

/**
 * Deep-clone a plain JSON-ish value. We round-trip through JSON so the
 * returned object owns no aliased references — callers can mutate it
 * freely. Functions / Maps / cycles are NOT supported (and not present
 * in Charter objects).
 */
function deepCloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/**
 * Build the canonical-bytes payload for a Charter.
 *
 * Accepts either a parsed `Charter` (from `parseCharter`) or any raw
 * dict-like object whose shape matches a Charter. The function does
 * NOT validate — pass invalid input and you'll get garbage canonical
 * bytes. Validate first via `parseCharter` if the input is untrusted.
 *
 * The Charter argument is NEVER mutated; we deep-clone before applying
 * the §1 transformations.
 */
export function canonicalBytes(charter: Charter | Record<string, unknown>): Uint8Array {
  const clone = deepCloneJson(charter) as Record<string, unknown>;

  // §1.1 — clear signature + transparency_log_id.
  const provenance = clone["provenance"] as Record<string, unknown> | undefined;
  if (provenance !== undefined && provenance !== null) {
    provenance["issuer_signature"] = "";
    provenance["transparency_log_id"] = null;
  }

  // §1.2 — elide private_fields=null on each clause.
  const clauses = clone["clauses"];
  if (Array.isArray(clauses)) {
    for (const c of clauses) {
      if (c !== null && typeof c === "object") {
        const clause = c as Record<string, unknown>;
        const pf = clause["private_fields"];
        if (pf === null || pf === undefined) {
          delete clause["private_fields"];
        }
      }
    }
  }

  // §1.3 — sorted keys + compact JSON + UTF-8 bytes.
  const sorted = sortKeysDeep(clone);
  const serialized = JSON.stringify(sorted);
  return new TextEncoder().encode(serialized);
}

/**
 * Convenience: SHA-256 of `canonicalBytes(charter)` as `sha256:<hex>`.
 *
 * Implemented via `node:crypto` so we don't pull a hash library for a
 * one-line primitive. WebCrypto would work the same; we stay sync here
 * because the rest of the canonical path is sync.
 */
export async function canonicalBytesSha256(
  charter: Charter | Record<string, unknown>,
): Promise<string> {
  const { createHash } = await import("node:crypto");
  const digest = createHash("sha256").update(canonicalBytes(charter)).digest("hex");
  return `sha256:${digest}`;
}
