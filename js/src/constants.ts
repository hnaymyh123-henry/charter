/**
 * Charter protocol constants.
 *
 * These values are part of the protocol contract — they cannot be changed
 * at runtime. Byte-for-byte equivalent to `charter.constants` in the
 * Python reference implementation, anchored by SPEC.md §3.
 */

export type Decision = "allow" | "needs_approval" | "incompatible";

export type ClauseType =
  | "scope"
  | "out_of_scope"
  | "approval_required"
  | "operational_limit"
  | "style"
  | "data_handling";

/**
 * Per-clause local-decision mapping (SPEC.md §3, protocol invariant #2).
 * A clause's type deterministically maps to a local decision; the LLM
 * grader only judges whether the clause is hit, never the decision.
 *
 * MUST mirror Python `charter.constants.TYPE_TO_DECISION` exactly.
 */
export const TYPE_TO_DECISION: Readonly<Record<ClauseType, Decision>> = Object.freeze({
  scope: "allow",
  out_of_scope: "incompatible",
  approval_required: "needs_approval",
  operational_limit: "needs_approval",
  style: "allow",
  data_handling: "needs_approval",
});

// Aggregate-decision precedence (SPEC.md §4.1).
// incompatible > needs_approval > allow.
const DECISION_RANK: Readonly<Record<Decision, number>> = Object.freeze({
  allow: 0,
  needs_approval: 1,
  incompatible: 2,
});

export const CHARTER_VERSION = "0.1";
export const DEFAULT_VALID_DAYS = 30;
export const LOW_CONFIDENCE_THRESHOLD = 0.5;

// Internal — exported for sibling modules; not part of the public API.
export function decisionRank(d: Decision): number {
  return DECISION_RANK[d];
}
