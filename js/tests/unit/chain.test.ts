/**
 * Strict chain verification tests (SPEC.md §6.1).
 *
 * Covers the four rules:
 *   1. attenuation_proof.parent_charter_id must match parent.charter_id
 *      (when set).
 *   2. Each parent `out_of_scope` clause is covered by a child clause
 *      (text-equal OR child-superset).
 *   3. Each parent `approval_required` clause is covered.
 *   4. Each child `scope` clause matches some parent `scope` clause
 *      exactly (no widening).
 */

import { describe, it, expect } from "vitest";

import { verifyChain, verifyChainStrict } from "../../src/chain.js";
import type { Charter, Clause } from "../../src/schema.js";
import { minimalCharter } from "./fixtures.js";

function clause(id: string, type: Clause["type"], text: string): Clause {
  return { id, type, text, private_fields: null };
}

function setClauses(c: Charter, clauses: Clause[]): Charter {
  return { ...c, clauses };
}

describe("verifyChainStrict", () => {
  it("passes when child and parent are identical", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "scope", "Read documents"),
      clause("P2", "out_of_scope", "Send email"),
    ]);
    const child = setClauses(
      { ...parent, charter_id: "child" },
      [clause("C1", "scope", "Read documents"), clause("C2", "out_of_scope", "Send email")],
    );
    expect(verifyChainStrict(child, parent)).toBe(true);
  });

  it("fails when attenuation_proof.parent_charter_id does not match parent.charter_id", () => {
    const parent = minimalCharter();
    const child = {
      ...minimalCharter(),
      charter_id: "child",
      attenuation_proof: {
        parent_charter_id: "wrong-parent",
        attenuates: {},
        semantic_check_cache: {},
      },
    };
    expect(verifyChainStrict(child, parent)).toBe(false);
  });

  it("passes when attenuation_proof.parent_charter_id matches", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "scope", "Read documents"),
    ]);
    const child: Charter = {
      ...setClauses(minimalCharter(), [clause("C1", "scope", "Read documents")]),
      charter_id: "child",
      attenuation_proof: {
        parent_charter_id: parent.charter_id,
        attenuates: {},
        semantic_check_cache: {},
      },
    };
    expect(verifyChainStrict(child, parent)).toBe(true);
  });

  it("fails when parent out_of_scope is NOT echoed by child", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "out_of_scope", "Send marketing email"),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, []);
    expect(verifyChainStrict(child, parent)).toBe(false);
  });

  it("passes when parent out_of_scope is a substring of a child out_of_scope (tighter)", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "out_of_scope", "Send email"),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, [
      clause("C1", "out_of_scope", "Send email to external contacts"),
    ]);
    expect(verifyChainStrict(child, parent)).toBe(true);
  });

  it("fails when parent approval_required is NOT echoed by child", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "approval_required", "Spend > $100"),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, []);
    expect(verifyChainStrict(child, parent)).toBe(false);
  });

  it("fails when child has a scope clause not present in parent (no widening)", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "scope", "Read documents"),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, [
      clause("C1", "scope", "Read documents"),
      clause("C2", "scope", "Send email"), // not in parent
    ]);
    expect(verifyChainStrict(child, parent)).toBe(false);
  });

  it("ignores style / operational_limit / data_handling clauses in either side", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "operational_limit", "Rate-limit X"),
      clause("P2", "style", "Be concise"),
      clause("P3", "data_handling", "PII redaction"),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, []);
    // No scope / out_of_scope / approval_required on either side → vacuously OK.
    expect(verifyChainStrict(child, parent)).toBe(true);
  });

  it("trims whitespace when comparing texts (matches Python str.strip())", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "scope", "  Read documents  "),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, [
      clause("C1", "scope", "Read documents"),
    ]);
    expect(verifyChainStrict(child, parent)).toBe(true);
  });
});

describe("verifyChain mode dispatch", () => {
  it("strict and auto both run the strict path (semantic is not implemented)", () => {
    const parent = setClauses(minimalCharter(), [
      clause("P1", "scope", "Read"),
    ]);
    const child = setClauses({ ...minimalCharter(), charter_id: "child" }, [
      clause("C1", "scope", "Read"),
    ]);
    expect(verifyChain(child, parent, "strict")).toBe(true);
    expect(verifyChain(child, parent, "auto")).toBe(true);
  });

  it("throws on an unknown mode", () => {
    const parent = minimalCharter();
    const child = { ...minimalCharter(), charter_id: "child" };
    expect(() => verifyChain(child, parent, "semantic" as any)).toThrow();
  });
});
