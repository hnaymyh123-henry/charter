/**
 * Lifecycle state-machine policy classifier (SPEC.md §5).
 *
 * The Charter `lifecycle.status` is issuer-driven; the protocol does NOT
 * enforce a state machine at verification time. The policy layer DOES,
 * and turns the status (plus `valid_until` vs now) into one of four
 * classifications used by host policy code.
 *
 * Mirrors what the Python conformance runner's `_op_lifecycle_status`
 * computes — see `conformance/runners/python/run.py` and SPEC.md §5.
 */

import type { Charter } from "./schema.js";

export type PolicyClassification =
  | "usable"
  | "needs_approval_or_incompatible"
  | "incompatible"
  | "redirect_to_successor";

export interface LifecycleStatusResult {
  status: Charter["lifecycle"]["status"];
  is_expired: boolean;
  is_revoked: boolean;
  is_superseded: boolean;
  policy_classification: PolicyClassification;
}

/**
 * Classify a Charter's lifecycle as of `now`.
 *
 * `now` may be a `Date` or an ISO-8601 string — both are accepted so
 * the conformance runner can pass the vector's `now_iso` straight in.
 *
 * Priority is the same as Python:
 *   1. revoked → incompatible (takes precedence over everything).
 *   2. superseded (status === "superseded" OR `replaced_by` populated)
 *      AND not revoked → redirect_to_successor.
 *   3. expired (valid_until < now) → needs_approval_or_incompatible.
 *   4. otherwise → usable.
 */
export function lifecycleStatus(
  charter: Charter,
  now: Date | string,
): LifecycleStatusResult {
  const nowDate = typeof now === "string" ? new Date(now) : now;
  if (Number.isNaN(nowDate.getTime())) {
    throw new Error(`lifecycleStatus: invalid 'now' input ${JSON.stringify(now)}`);
  }
  const validUntil = new Date(charter.lifecycle.valid_until);
  if (Number.isNaN(validUntil.getTime())) {
    throw new Error(
      `lifecycleStatus: invalid valid_until ${JSON.stringify(charter.lifecycle.valid_until)}`,
    );
  }

  const status = charter.lifecycle.status;
  const is_revoked = status === "revoked";
  const is_superseded =
    status === "superseded" || charter.lifecycle.replaced_by !== null;
  const is_expired = validUntil.getTime() < nowDate.getTime() && !is_revoked;

  let policy_classification: PolicyClassification;
  if (is_revoked) {
    policy_classification = "incompatible";
  } else if (is_superseded && !is_revoked) {
    policy_classification = "redirect_to_successor";
  } else if (is_expired) {
    policy_classification = "needs_approval_or_incompatible";
  } else {
    policy_classification = "usable";
  }

  return {
    status,
    is_expired,
    is_revoked,
    is_superseded,
    policy_classification,
  };
}
