/**
 * Public-API smoke test.
 *
 * Imports through `@charter/core` (resolved to src/index.ts in tests) and
 * verifies the headline exports are reachable. If a future refactor
 * silently drops an export, this test fails. Cheaper than relying on
 * downstream consumers to notice.
 */

import { describe, it, expect } from "vitest";

import * as charter from "../../src/index.js";

describe("@charter/core public exports", () => {
  it("exports the schema, sign/verify, canonical, aggregate primitives", () => {
    expect(typeof charter.parseCharter).toBe("function");
    expect(typeof charter.signCharter).toBe("function");
    expect(typeof charter.verifyCharter).toBe("function");
    expect(typeof charter.canonicalBytes).toBe("function");
    expect(typeof charter.canonicalBytesSha256).toBe("function");
    expect(typeof charter.aggregateDecision).toBe("function");
    expect(typeof charter.aggregateVerdict).toBe("function");
    expect(typeof charter.typeToDecision).toBe("function");
  });

  it("exports the chain, lifecycle, jwks, pins primitives", () => {
    expect(typeof charter.verifyChain).toBe("function");
    expect(typeof charter.verifyChainStrict).toBe("function");
    expect(typeof charter.lifecycleStatus).toBe("function");
    expect(typeof charter.kidForPublicKey).toBe("function");
    expect(typeof charter.publicKeyToJwk).toBe("function");
    expect(typeof charter.fingerprintOf).toBe("function");
  });

  it("exports the transparency, privacy primitives", () => {
    expect(typeof charter.verifyLogChain).toBe("function");
    expect(charter.GENESIS_PREV_HASH).toMatch(/^sha256:0+$/);
    expect(typeof charter.redactClause).toBe("function");
    expect(typeof charter.verifyDisclosure).toBe("function");
    expect(typeof charter.matchRedacted).toBe("function");
  });

  it("exports the protocol constants", () => {
    expect(charter.CHARTER_VERSION).toBe("0.1");
    expect(charter.TYPE_TO_DECISION).toBeDefined();
    expect(charter.TYPE_TO_DECISION.scope).toBe("allow");
  });
});
