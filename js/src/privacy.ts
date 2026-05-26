/**
 * Per-span redaction with SD-JWT-style selective disclosure
 * (SPEC.md §10, ADR-011 path 1).
 *
 * Sensitive substrings of a clause's `text` are replaced by
 * `[REDACTED:<hash-prefix>]` placeholders. The matching plaintext
 * lives in a separate `Disclosure` record — the Charter itself only
 * commits to the SHA-256 of `salt || plaintext`.
 *
 * Caller-side: `verifyDisclosure` confirms a fetched Disclosure
 * matches the hash in `PrivateFieldRef`; `matchRedacted` answers
 * "is this candidate value the plaintext behind any redacted span?"
 * without revealing which.
 */

import { createHash } from "node:crypto";
import { randomBytes } from "node:crypto";

import type { Disclosure, PrivateFieldRef } from "./schema.js";

// Mirror Python `charter.privacy` constants exactly.
export const SALT_BYTES = 16;
export const HASH_PREFIX_HEX_CHARS = 8;

// ---------------------------------------------------------------------------
// Hash + placeholder helpers
// ---------------------------------------------------------------------------

function hashValue(salt: Uint8Array, value: string): string {
  const buf = Buffer.concat([Buffer.from(salt), Buffer.from(value, "utf-8")]);
  const hex = createHash("sha256").update(buf).digest("hex");
  return `sha256:${hex}`;
}

function placeholderFor(disclosureHash: string): string {
  if (!disclosureHash.startsWith("sha256:")) {
    throw new Error("disclosure_hash must start with 'sha256:'");
  }
  const prefix = disclosureHash.slice("sha256:".length, "sha256:".length + HASH_PREFIX_HEX_CHARS);
  return `[REDACTED:${prefix}]`;
}

function makeDisclosureId(salt: Uint8Array, value: string): string {
  const buf = Buffer.concat([Buffer.from(salt), Buffer.from(value, "utf-8")]);
  const hex = createHash("sha256").update(buf).digest("hex");
  return hex.slice(0, HASH_PREFIX_HEX_CHARS);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface RedactResult {
  redacted_text: string;
  private_fields: PrivateFieldRef[];
  disclosures: Disclosure[];
}

/**
 * Build the redacted clause text + the public + private artefacts.
 *
 * @param clauseText   The original (sensitive) clause text.
 * @param privateSpans Half-open `[start, end)` intervals into
 *                     `clauseText`. MUST be non-overlapping; we sort
 *                     them defensively and throw on overlap.
 * @param salt         Test-only deterministic salt. When provided, ALL
 *                     spans share it (matching Python `redact_clause`'s
 *                     test contract). When omitted, a fresh
 *                     `SALT_BYTES`-byte salt is drawn per span.
 *
 * Output coordinates inside each `PrivateFieldRef` point INTO the
 * returned `redacted_text` (NOT the input), so callers can locate the
 * placeholder without reconstructing the source.
 */
export function redactClause(
  clauseText: string,
  privateSpans: ReadonlyArray<readonly [number, number]>,
  salt?: Uint8Array,
): RedactResult {
  if (privateSpans.length === 0) {
    return { redacted_text: clauseText, private_fields: [], disclosures: [] };
  }

  // Sort by start ascending so we can rebuild the text in one pass.
  const spans = [...privateSpans]
    .map((s) => [s[0], s[1]] as [number, number])
    .sort((a, b) => a[0] - b[0]);

  for (let i = 0; i < spans.length - 1; i++) {
    if ((spans[i] as [number, number])[1] > (spans[i + 1] as [number, number])[0]) {
      throw new Error(
        `overlapping private spans: ${JSON.stringify(spans[i])} and ${JSON.stringify(spans[i + 1])}; ` +
          "ADR-011 path 1 requires non-overlapping redactions",
      );
    }
  }

  const outParts: string[] = [];
  const privateFields: PrivateFieldRef[] = [];
  const disclosures: Disclosure[] = [];
  const seenIds = new Set<string>();
  let cursor = 0;

  for (const [start, end] of spans) {
    if (start < 0 || end > clauseText.length || start >= end) {
      throw new Error(
        `invalid span (${start}, ${end}) for clause of length ${clauseText.length}`,
      );
    }
    const value = clauseText.slice(start, end);
    const spanSalt = salt ?? new Uint8Array(randomBytes(SALT_BYTES));
    const disclosureHash = hashValue(spanSalt, value);
    const placeholder = placeholderFor(disclosureHash);

    outParts.push(clauseText.slice(cursor, start));
    const redactedSoFarLen = outParts.reduce((acc, p) => acc + p.length, 0);
    outParts.push(placeholder);
    cursor = end;

    const baseId = makeDisclosureId(spanSalt, value);
    let uniqueId = baseId;
    let suffix = 1;
    while (seenIds.has(uniqueId)) {
      uniqueId = `${baseId}-${suffix}`;
      suffix++;
    }
    seenIds.add(uniqueId);

    privateFields.push({
      span_start: redactedSoFarLen,
      span_end: redactedSoFarLen + placeholder.length,
      disclosure_hash: disclosureHash,
    });
    disclosures.push({
      disclosure_id: uniqueId,
      span_value: value,
      salt_hex: Buffer.from(spanSalt).toString("hex"),
      disclosure_hash: disclosureHash,
    });
  }

  outParts.push(clauseText.slice(cursor));
  return {
    redacted_text: outParts.join(""),
    private_fields: privateFields,
    disclosures,
  };
}

/**
 * Confirm that a fetched `Disclosure` reproduces the hash committed in
 * the matching `PrivateFieldRef`.
 *
 * Returns `true` iff `sha256(salt || span_value)` equals BOTH
 * `claimedHash` and the `disclosure_hash` field on the disclosure
 * itself. Any tampering (value swap, salt edit, hash forge) flips this
 * to false. Returns `false` (not throw) on decode errors so verification
 * code can act on a single boolean.
 */
export function verifyDisclosure(
  disclosure: Disclosure,
  claimedHash: string,
): boolean {
  let salt: Uint8Array;
  try {
    salt = new Uint8Array(Buffer.from(disclosure.salt_hex, "hex"));
    // Detect "not actually hex" inputs (Buffer.from drops invalid chars).
    if (salt.length === 0 && disclosure.salt_hex.length > 0) {
      return false;
    }
    // Detect odd-length / non-hex inputs that Buffer.from silently truncated.
    if (salt.length * 2 !== disclosure.salt_hex.length) {
      return false;
    }
  } catch {
    return false;
  }
  const recomputed = hashValue(salt, disclosure.span_value);
  return recomputed === claimedHash && recomputed === disclosure.disclosure_hash;
}

/**
 * True iff `candidateValue` is the plaintext behind any redacted span
 * present in `clauseText`.
 *
 * Designed for the caller-side "does this charter pertain to candidate
 * value Foo?" check. Deliberately returns ONLY a boolean (not which
 * disclosure matched) so a probe attacker cannot enumerate the full
 * disclosure set through a side channel.
 *
 * A disclosure whose placeholder is NOT present in `clauseText` is
 * skipped — pinning the match to clauses prevents leakage of values
 * from unrelated clauses.
 */
export function matchRedacted(
  clauseText: string,
  candidateValue: string,
  disclosures: readonly Disclosure[],
): boolean {
  for (const disc of disclosures) {
    const placeholder = placeholderFor(disc.disclosure_hash);
    if (!clauseText.includes(placeholder)) {
      continue;
    }
    let salt: Uint8Array;
    try {
      salt = new Uint8Array(Buffer.from(disc.salt_hex, "hex"));
      if (salt.length * 2 !== disc.salt_hex.length) {
        continue;
      }
    } catch {
      continue;
    }
    if (hashValue(salt, candidateValue) === disc.disclosure_hash) {
      return true;
    }
  }
  return false;
}
