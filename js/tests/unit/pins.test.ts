/**
 * Pin fingerprint tests (SPEC.md §9).
 *
 * The fingerprint is the FULL sha256:<64-hex> digest of the raw public
 * key bytes — no truncation. Tested for determinism and uniqueness.
 */

import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";

import { fingerprintOf } from "../../src/pins.js";
import { publicKeyFromSeed, publicKeyToString } from "../../src/signing.js";
import { TEST_SEED } from "./fixtures.js";

describe("fingerprintOf", () => {
  it("returns sha256:<64-hex>", async () => {
    const pub = publicKeyToString(await publicKeyFromSeed(TEST_SEED));
    const fp = fingerprintOf(pub);
    expect(fp).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it("is the full sha256 of raw public key bytes", async () => {
    const pub = publicKeyToString(await publicKeyFromSeed(TEST_SEED));
    const raw = Buffer.from(pub.slice("ed25519:".length), "base64");
    const expected = "sha256:" + createHash("sha256").update(raw).digest("hex");
    expect(fingerprintOf(pub)).toBe(expected);
  });

  it("is deterministic", async () => {
    const pub = publicKeyToString(await publicKeyFromSeed(TEST_SEED));
    expect(fingerprintOf(pub)).toBe(fingerprintOf(pub));
  });

  it("differs across different keys", async () => {
    const pubA = publicKeyToString(await publicKeyFromSeed(TEST_SEED));
    const pubB = publicKeyToString(
      await publicKeyFromSeed(new Uint8Array(32).fill(0x66)),
    );
    expect(fingerprintOf(pubA)).not.toBe(fingerprintOf(pubB));
  });
});
