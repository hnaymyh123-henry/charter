/**
 * Aggregate verdict tests.
 *
 * Covers protocol invariant #3 (incompatible > needs_approval > allow)
 * and the closed-world fallback (empty list → needs_approval, NOT allow).
 */

import { describe, it, expect } from "vitest";

import { aggregateDecision, aggregateVerdict, typeToDecision } from "../../src/aggregate.js";
import type { MatchedClause } from "../../src/schema.js";

function mc(
  id: string,
  decision: MatchedClause["local_decision"],
  applied = true,
  confidence = 0.9,
): MatchedClause {
  return {
    id,
    local_decision: decision,
    applied,
    confidence,
    reason: `clause ${id}`,
    source_charter_id: null,
  };
}

describe("aggregateDecision precedence (invariant #3)", () => {
  it("allow + needs_approval → needs_approval", () => {
    expect(aggregateDecision(["allow", "needs_approval"])).toBe("needs_approval");
  });

  it("needs_approval + incompatible → incompatible", () => {
    expect(aggregateDecision(["needs_approval", "incompatible"])).toBe("incompatible");
  });

  it("any incompatible dominates the entire list", () => {
    expect(
      aggregateDecision(["allow", "allow", "incompatible", "needs_approval"]),
    ).toBe("incompatible");
  });

  it("all allows → allow", () => {
    expect(aggregateDecision(["allow", "allow", "allow"])).toBe("allow");
  });

  it("empty list → needs_approval (closed-world fallback, NOT allow)", () => {
    expect(aggregateDecision([])).toBe("needs_approval");
  });
});

describe("typeToDecision (invariant #2)", () => {
  it("scope and style map to allow", () => {
    expect(typeToDecision("scope")).toBe("allow");
    expect(typeToDecision("style")).toBe("allow");
  });

  it("out_of_scope maps to incompatible", () => {
    expect(typeToDecision("out_of_scope")).toBe("incompatible");
  });

  it("approval_required, operational_limit, data_handling map to needs_approval", () => {
    expect(typeToDecision("approval_required")).toBe("needs_approval");
    expect(typeToDecision("operational_limit")).toBe("needs_approval");
    expect(typeToDecision("data_handling")).toBe("needs_approval");
  });
});

describe("aggregateVerdict", () => {
  it("marks ONLY clauses with the winning local_decision as applied", () => {
    const v = aggregateVerdict([
      mc("a", "allow"),
      mc("b", "needs_approval"),
      mc("c", "incompatible"),
    ]);
    expect(v.decision).toBe("incompatible");
    const applied = v.matched_clauses.filter((m) => m.applied).map((m) => m.id);
    expect(applied).toEqual(["c"]);
  });

  it("preserves all input clauses (just toggles applied)", () => {
    const v = aggregateVerdict([mc("a", "allow"), mc("b", "needs_approval")]);
    expect(v.matched_clauses).toHaveLength(2);
  });

  it("rewrite_available is true iff aggregate is incompatible", () => {
    expect(aggregateVerdict([mc("a", "incompatible")]).rewrite_available).toBe(true);
    expect(aggregateVerdict([mc("a", "needs_approval")]).rewrite_available).toBe(false);
    expect(aggregateVerdict([mc("a", "allow")]).rewrite_available).toBe(false);
  });

  it("reason concatenates ONLY applied-clause reasons", () => {
    const v = aggregateVerdict([
      mc("a", "allow"),
      mc("b", "incompatible"),
      mc("c", "incompatible"),
    ]);
    expect(v.reason).toContain("[b]");
    expect(v.reason).toContain("[c]");
    expect(v.reason).not.toContain("[a]");
  });

  it("empty list produces closed-world fallback reason + needs_approval", () => {
    const v = aggregateVerdict([]);
    expect(v.decision).toBe("needs_approval");
    expect(v.reason).toMatch(/closed-world/);
    expect(v.matched_clauses).toEqual([]);
  });
});
