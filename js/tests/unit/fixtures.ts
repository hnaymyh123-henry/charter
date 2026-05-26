/**
 * Shared test fixtures for unit tests.
 *
 * Returns a fresh minimal Charter on every call so individual tests can
 * mutate freely without leaking state. The base shape mirrors
 * `conformance/vectors/sign/canonical_bytes_minimal.json` (the cross-
 * language source of truth), with one important difference: we strip the
 * one `source_commitment` so callers can re-add their own and avoid hash
 * collisions across tests that diff on canonical bytes.
 */

import type { Charter } from "../../src/schema.js";

export function minimalCharter(): Charter {
  return {
    version: "0.1",
    charter_id: "charter:alice@acme.com:research_agent:2026-05-23",
    binding: {
      type: "principal_agent",
      principal_id: "alice@acme.com",
      agent_id: "research_agent",
    },
    principal: {
      type: "human",
      id: "alice@acme.com",
      role_summary: "Researcher at Acme Corp.",
    },
    issuer: {
      type: "human",
      id: "alice@acme.com",
      relationship_to_principal: "self",
    },
    agent_operator: {
      type: "service",
      id: "acme_research_operator",
      agent_card_url: null,
    },
    principal_chain: [],
    visibility: {
      charter: "public",
      raw_principal_context: "private",
      private_clauses: "not_supported_in_v0",
    },
    summary: {
      plain_language: "A research agent operating within Alice's scope.",
    },
    clauses: [
      {
        id: "C-001",
        type: "scope",
        text: "Research scientific literature.",
        private_fields: null,
      },
      {
        id: "C-002",
        type: "out_of_scope",
        text: "Marketing copy.",
        private_fields: null,
      },
    ],
    decision_schema: {
      decision: "allow | needs_approval | incompatible",
      matched_clauses:
        "[{id, local_decision, applied, confidence in [0,1], reason}]",
      reason: "string -- short summary referencing applied clauses",
      rewrite_available: "bool -- whether propose_within_scope might help",
    },
    lifecycle: {
      issued_at: "2026-05-23T12:00:00Z",
      valid_until: "2026-06-22T12:00:00Z",
      status: "active",
      revoked_at: null,
      replaces: null,
      replaced_by: null,
    },
    provenance: {
      issuer_public_key: "ed25519:iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w=",
      issuer_signature: "",
      issuer_kid: null,
      transparency_log_id: null,
      source_commitments: [],
      generated_at: "2026-05-23T12:00:00Z",
    },
    parent_charter_url: null,
    attenuation_proof: null,
  };
}

/**
 * Deterministic 32-byte Ed25519 seed for sign/verify tests. NOT secret;
 * NOT used outside tests. Any 32 bytes works — we pick a recognisable
 * pattern so a failing diff shows where in the test the seed leaked.
 */
export const TEST_SEED = new Uint8Array(32).map((_, i) => (i + 1) & 0xff);
