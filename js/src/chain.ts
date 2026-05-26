/**
 * Charter chain attenuation — strict (string-based) verifier.
 *
 * Per Issue #50 scope, this SDK ships ONLY the string-based path. The
 * semantic (LLM-graded) mode is deliberately omitted — JS has no
 * counterpart to Python's `GraderClient` injection point and there's
 * no portable way to thread an LLM client through the conformance
 * suite. `mode="auto"` therefore degrades to `"strict"` here.
 *
 * Rules (SPEC.md §6.1):
 *   1. If `child.attenuation_proof.parent_charter_id` is set, it MUST
 *      equal `parent.charter_id`.
 *   2. Every `out_of_scope` clause in `parent` is covered by some
 *      `out_of_scope` clause in `child`. Coverage = text equality OR
 *      parent's stripped text is a substring of child's stripped text.
 *   3. Same rule for every `approval_required` clause.
 *   4. Every `scope` clause in `child` matches some `scope` clause in
 *      `parent` by exact stripped equality (child cannot widen scope).
 *   5. `operational_limit`, `style`, `data_handling` are NOT checked.
 */

import type { Charter, Clause } from "./schema.js";

export type ChainMode = "strict" | "auto";

function strip(s: string): string {
  // Mirror Python `str.strip()` — trims ASCII whitespace including
  // \t \n \v \f \r. JavaScript's `String.prototype.trim` follows the
  // ECMAScript WhiteSpace + LineTerminator spec, which is a superset
  // of Python's default. For Charter clause text (which doesn't carry
  // exotic Unicode whitespace), they agree byte-for-byte.
  return s.trim();
}

function covers(childClause: Clause, parentClause: Clause): boolean {
  const pt = strip(parentClause.text);
  const ct = strip(childClause.text);
  return pt === ct || ct.includes(pt);
}

function anyCovers(childClauses: readonly Clause[], parentClause: Clause): boolean {
  for (const cc of childClauses) {
    if (covers(cc, parentClause)) {
      return true;
    }
  }
  return false;
}

/**
 * `True` iff `child` is a valid attenuation of `parent` under the
 * strict string-based rules.
 *
 * The `mode` parameter accepts `"strict"` and `"auto"`; both run the
 * same checks because semantic mode is not implemented in JS (Issue
 * #50 scope decision; LLM-injected graders are an `@charter/server`
 * concern).
 */
export function verifyChain(
  child: Charter,
  parent: Charter,
  mode: ChainMode = "strict",
): boolean {
  if (mode !== "strict" && mode !== "auto") {
    throw new Error(`verifyChain: unknown mode ${JSON.stringify(mode)}`);
  }
  return verifyChainStrict(child, parent);
}

/**
 * Strict-only public API — direct counterpart to Python's
 * `_verify_chain_strict`. Exposed so the conformance runner can dispatch
 * the `verify_chain_strict` operation without going through `verifyChain`.
 */
export function verifyChainStrict(child: Charter, parent: Charter): boolean {
  const parentId = parent.charter_id;

  // 1. attenuation_proof claim
  if (child.attenuation_proof !== null && child.attenuation_proof !== undefined) {
    if (child.attenuation_proof.parent_charter_id !== parentId) {
      return false;
    }
  }

  // 2. out_of_scope coverage
  const parentOOS = parent.clauses.filter((c) => c.type === "out_of_scope");
  const childOOS = child.clauses.filter((c) => c.type === "out_of_scope");
  for (const pc of parentOOS) {
    if (!anyCovers(childOOS, pc)) {
      return false;
    }
  }

  // 3. approval_required coverage
  const parentAppr = parent.clauses.filter((c) => c.type === "approval_required");
  const childAppr = child.clauses.filter((c) => c.type === "approval_required");
  for (const pc of parentAppr) {
    if (!anyCovers(childAppr, pc)) {
      return false;
    }
  }

  // 4. scope subset — every child scope clause MUST equal some parent
  // scope clause exactly. Superstring is NOT allowed here (child cannot
  // self-grant new authority).
  const parentScopeTexts = new Set(
    parent.clauses.filter((c) => c.type === "scope").map((c) => strip(c.text)),
  );
  for (const cc of child.clauses) {
    if (cc.type === "scope" && !parentScopeTexts.has(strip(cc.text))) {
      return false;
    }
  }

  return true;
}
