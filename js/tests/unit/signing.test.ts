/**
 * Ed25519 sign / verify tests.
 *
 * Roundtrip: signCharter then verifyCharter passes; flipping any byte of
 * the signed payload or swapping in a wrong key fails. Per ADR-002, any
 * non-`ed25519:` prefix MUST fail verification (no silent algorithm
 * widening).
 */

import { describe, it, expect } from "vitest";
import * as ed from "@noble/ed25519";

import { signCharter, verifyCharter, publicKeyFromString, publicKeyToString, publicKeyFromSeed } from "../../src/signing.js";
import { minimalCharter, TEST_SEED } from "./fixtures.js";

describe("signCharter + verifyCharter", () => {
  it("signs and verifies the minimal Charter (happy path)", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);

    const signed = await signCharter(c, TEST_SEED);
    expect(signed.provenance.issuer_signature).toMatch(/^ed25519:/);
    expect(await verifyCharter(signed)).toBe(true);
  });

  it("does not mutate the input Charter", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);
    const snapshot = JSON.stringify(c);
    await signCharter(c, TEST_SEED);
    expect(JSON.stringify(c)).toBe(snapshot);
  });

  it("populates issuer_kid from the public key when missing", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);
    c.provenance.issuer_kid = null;

    const signed = await signCharter(c, TEST_SEED);
    expect(signed.provenance.issuer_kid).toMatch(/^[0-9a-f]{16}$/);
  });

  it("verifyCharter returns false when the signature is tampered", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);

    const signed = await signCharter(c, TEST_SEED);
    const sigBody = signed.provenance.issuer_signature.slice("ed25519:".length);
    // Flip a base64 char and re-prefix.
    const tamperedBase = sigBody[0] === "A" ? "B" + sigBody.slice(1) : "A" + sigBody.slice(1);
    signed.provenance.issuer_signature = "ed25519:" + tamperedBase;
    expect(await verifyCharter(signed)).toBe(false);
  });

  it("verifyCharter returns false when the payload is tampered", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);

    const signed = await signCharter(c, TEST_SEED);
    // Change an unsigned field is not possible (issuer_signature & log_id
    // are cleared by canonical bytes), so we change a signed field:
    signed.summary.plain_language = "evil rewrite";
    expect(await verifyCharter(signed)).toBe(false);
  });

  it("verifyCharter returns false against a wrong public key", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);
    const signed = await signCharter(c, TEST_SEED);

    // Swap in a different valid public key
    const otherSeed = new Uint8Array(32).fill(0x42);
    const otherPub = await publicKeyFromSeed(otherSeed);
    signed.provenance.issuer_public_key = publicKeyToString(otherPub);
    expect(await verifyCharter(signed)).toBe(false);
  });

  it("verifyCharter rejects non-ed25519 signature prefix (ADR-002)", async () => {
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);
    const signed = await signCharter(c, TEST_SEED);

    // Swap the prefix
    const body = signed.provenance.issuer_signature.slice("ed25519:".length);
    signed.provenance.issuer_signature = "rsa256:" + body;
    expect(await verifyCharter(signed)).toBe(false);
  });

  it("rejects a 32-byte seed of the wrong length when signing", async () => {
    const c = minimalCharter();
    await expect(signCharter(c, new Uint8Array(31))).rejects.toThrow(/32 bytes/);
  });

  it("verifies bytes-equivalent payload to the standalone ed.verifyAsync primitive", async () => {
    // Ensures our canonical-bytes path is the one being signed (i.e., we
    // don't accidentally sign a different serialization).
    const c = minimalCharter();
    const pubBytes = await publicKeyFromSeed(TEST_SEED);
    c.provenance.issuer_public_key = publicKeyToString(pubBytes);
    const signed = await signCharter(c, TEST_SEED);

    const { canonicalBytes } = await import("../../src/canonical.js");
    const sig = signed.provenance.issuer_signature.slice("ed25519:".length);
    const sigBytes = Buffer.from(sig, "base64");
    const payload = canonicalBytes(signed);
    expect(await ed.verifyAsync(new Uint8Array(sigBytes), payload, pubBytes)).toBe(true);
  });
});

describe("public key codec", () => {
  it("roundtrips ed25519:<base64> → bytes → ed25519:<base64>", async () => {
    const pub = await publicKeyFromSeed(TEST_SEED);
    const s = publicKeyToString(pub);
    expect(s).toMatch(/^ed25519:/);
    const back = publicKeyFromString(s);
    expect(Buffer.from(back).equals(Buffer.from(pub))).toBe(true);
  });

  it("rejects a public key without the ed25519: prefix", () => {
    expect(() => publicKeyFromString("rsa256:abc")).toThrow(/ed25519:/);
  });
});
