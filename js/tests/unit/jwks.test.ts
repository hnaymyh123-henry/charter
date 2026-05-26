/**
 * JWKS kid + JWK shape tests (SPEC.md §7).
 *
 * Cross-language anchor: kid is deterministic, 16 lowercase-hex chars
 * derived from sha256(raw_public_key). Tested against the published
 * test key in the conformance vectors.
 */

import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";

import { kidForPublicKey, publicKeyToJwk } from "../../src/jwks.js";
import { publicKeyFromSeed, publicKeyToString } from "../../src/signing.js";
import { TEST_SEED } from "./fixtures.js";

const VECTOR_PUB = "ed25519:iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w=";

describe("kidForPublicKey", () => {
  it("returns 16 lowercase-hex chars", () => {
    const kid = kidForPublicKey(VECTOR_PUB);
    expect(kid).toMatch(/^[0-9a-f]{16}$/);
  });

  it("is the first 16 hex chars of sha256(raw_public_key)", () => {
    const raw = Buffer.from(VECTOR_PUB.slice("ed25519:".length), "base64");
    const expected = createHash("sha256").update(raw).digest("hex").slice(0, 16);
    expect(kidForPublicKey(VECTOR_PUB)).toBe(expected);
  });

  it("is deterministic across calls (no salt)", () => {
    expect(kidForPublicKey(VECTOR_PUB)).toBe(kidForPublicKey(VECTOR_PUB));
  });

  it("differs across different keys", async () => {
    const pubA = publicKeyToString(await publicKeyFromSeed(TEST_SEED));
    const pubB = publicKeyToString(
      await publicKeyFromSeed(new Uint8Array(32).fill(0x99)),
    );
    expect(kidForPublicKey(pubA)).not.toBe(kidForPublicKey(pubB));
  });
});

describe("publicKeyToJwk", () => {
  it("produces an RFC 7517 OKP JWK with all expected fields", () => {
    const j = publicKeyToJwk(VECTOR_PUB);
    expect(j.kty).toBe("OKP");
    expect(j.crv).toBe("Ed25519");
    expect(j.use).toBe("sig");
    expect(j.alg).toBe("EdDSA");
    expect(j.kid).toBe(kidForPublicKey(VECTOR_PUB));
    expect(j.x).toBeDefined();
  });

  it("encodes x as base64url without padding", () => {
    const j = publicKeyToJwk(VECTOR_PUB);
    expect(j.x).not.toContain("=");
    expect(j.x).not.toContain("+");
    expect(j.x).not.toContain("/");
    // 32 raw bytes → 43 base64url chars (no padding).
    expect(j.x.length).toBe(43);
  });

  it("allows kid override via options", () => {
    const j = publicKeyToJwk(VECTOR_PUB, { kid: "override-kid" });
    expect(j.kid).toBe("override-kid");
  });
});
