/**
 * Aggregate verdict computation (SPEC.md §4, protocol invariant #3).
 *
 * The aggregate decision is the MAX of per-clause local decisions
 * under the order `incompatible > needs_approval > allow`. Closed-world
 * fallback: an empty list collapses to `needs_approval` (NOT `allow`).
 */

import { decisionRank, TYPE_TO_DECISION } from "./constants.js";
import type { Decision, ClauseType } from "./constants.js";
import type { MatchedClause, Verdict } from "./schema.js";

/**
 * Combine per-clause local decisions into one aggregate decision.
 *
 * Mirrors Python `charter.constants.aggregate_decision`.
 */
export function aggregateDecision(localDecisions: readonly Decision[]): Decision {
  if (localDecisions.length === 0) {
    return "needs_approval";
  }
  let best: Decision = localDecisions[0] as Decision;
  for (const d of localDecisions) {
    if (decisionRank(d) > decisionRank(best)) {
      best = d;
    }
  }
  return best;
}

/**
 * Convenience: deterministic local decision lookup by clause type.
 * Same as indexing `TYPE_TO_DECISION` — exposed so external callers
 * don't need to import the constant object directly.
 */
export function typeToDecision(clauseType: ClauseType): Decision {
  return TYPE_TO_DECISION[clauseType];
}

/**
 * Build a Verdict from a list of MatchedClause entries.
 *
 * Mirrors what the Python implementation does at the end of
 * `check_compatibility`: take per-clause local decisions, aggregate
 * them, and mark the clauses that determined the aggregate as
 * `applied=true`. The decision is the aggregate; the reason is
 * the concatenation of all applied clause reasons.
 *
 * NOTE: This SDK is verification-only; we do NOT run an LLM grader
 * here. Callers feed already-graded MatchedClauses (e.g. computed
 * by their own grading pipeline or fetched from the Python server's
 * `/check_compatibility` endpoint) and we collapse them.
 */
export function aggregateVerdict(matchedClauses: readonly MatchedClause[]): Verdict {
  const decisions = matchedClauses.map((mc) => mc.local_decision);
  const aggregate = aggregateDecision(decisions);

  const appliedReasons: string[] = [];
  const annotated: MatchedClause[] = matchedClauses.map((mc) => ({
    ...mc,
    applied: mc.local_decision === aggregate,
  }));
  for (const mc of annotated) {
    if (mc.applied) {
      appliedReasons.push(`[${mc.id}] ${mc.reason}`);
    }
  }
  const reason =
    appliedReasons.length === 0
      ? "no clauses matched; conservative closed-world fallback"
      : appliedReasons.join("; ");

  return {
    decision: aggregate,
    matched_clauses: annotated,
    reason,
    rewrite_available: aggregate === "incompatible",
  };
}
