/**
 * Lifecycle policy-classifier tests (SPEC.md §5).
 *
 * Verifies the four classifications and their priority:
 *   1. revoked → incompatible (highest priority — wins over everything)
 *   2. superseded (and not revoked) → redirect_to_successor
 *   3. expired (valid_until < now) → needs_approval_or_incompatible
 *   4. otherwise → usable
 */

import { describe, it, expect } from "vitest";

import { lifecycleStatus } from "../../src/lifecycle.js";
import { minimalCharter } from "./fixtures.js";

const NOW_OK = "2026-05-30T00:00:00Z"; // between issued_at and valid_until
const NOW_AFTER = "2026-08-01T00:00:00Z"; // after valid_until

describe("lifecycleStatus — happy paths", () => {
  it("active + before valid_until → usable", () => {
    const r = lifecycleStatus(minimalCharter(), NOW_OK);
    expect(r.policy_classification).toBe("usable");
    expect(r.is_expired).toBe(false);
    expect(r.is_revoked).toBe(false);
    expect(r.is_superseded).toBe(false);
  });

  it("active + after valid_until → needs_approval_or_incompatible", () => {
    const r = lifecycleStatus(minimalCharter(), NOW_AFTER);
    expect(r.policy_classification).toBe("needs_approval_or_incompatible");
    expect(r.is_expired).toBe(true);
  });
});

describe("lifecycleStatus — revoked wins (priority 1)", () => {
  it("revoked → incompatible regardless of valid_until", () => {
    const c = minimalCharter();
    c.lifecycle.status = "revoked";
    c.lifecycle.revoked_at = "2026-05-24T00:00:00Z";
    const r = lifecycleStatus(c, NOW_OK);
    expect(r.policy_classification).toBe("incompatible");
    expect(r.is_revoked).toBe(true);
    expect(r.is_expired).toBe(false);
  });

  it("revoked beats superseded (revoke overrides redirect)", () => {
    const c = minimalCharter();
    c.lifecycle.status = "revoked";
    c.lifecycle.replaced_by = "charter:something_else";
    const r = lifecycleStatus(c, NOW_OK);
    expect(r.policy_classification).toBe("incompatible");
  });

  it("revoked beats expired", () => {
    const c = minimalCharter();
    c.lifecycle.status = "revoked";
    const r = lifecycleStatus(c, NOW_AFTER);
    expect(r.policy_classification).toBe("incompatible");
    expect(r.is_expired).toBe(false); // revoked short-circuits expired
  });
});

describe("lifecycleStatus — superseded (priority 2)", () => {
  it("status=superseded → redirect_to_successor", () => {
    const c = minimalCharter();
    c.lifecycle.status = "superseded";
    c.lifecycle.replaced_by = "charter:next";
    const r = lifecycleStatus(c, NOW_OK);
    expect(r.policy_classification).toBe("redirect_to_successor");
    expect(r.is_superseded).toBe(true);
  });

  it("replaced_by populated even when status=active → redirect_to_successor", () => {
    // Pythonic edge case — issuer set replaced_by but forgot to flip status.
    const c = minimalCharter();
    c.lifecycle.replaced_by = "charter:next";
    const r = lifecycleStatus(c, NOW_OK);
    expect(r.policy_classification).toBe("redirect_to_successor");
  });
});

describe("lifecycleStatus — Date input shape", () => {
  it("accepts a Date object as 'now'", () => {
    const r = lifecycleStatus(minimalCharter(), new Date(NOW_OK));
    expect(r.policy_classification).toBe("usable");
  });

  it("throws on an invalid now string", () => {
    expect(() => lifecycleStatus(minimalCharter(), "not-a-date")).toThrow(
      /invalid/,
    );
  });

  it("throws on an invalid valid_until in the Charter", () => {
    const c = minimalCharter();
    (c.lifecycle as any).valid_until = "garbage";
    expect(() => lifecycleStatus(c, NOW_OK)).toThrow(/valid_until/);
  });
});
