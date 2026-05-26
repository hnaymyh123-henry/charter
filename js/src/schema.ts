/**
 * Charter v0 schema in zod.
 *
 * Mirrors `charter.schema` (Pydantic v2) from the Python reference
 * implementation. Every model uses `.strict()` so unknown fields are
 * rejected — same behavior Pydantic gives with `model_config = ConfigDict(extra="forbid")`.
 *
 * Each exported `*Schema` constant is the zod schema; the matching
 * `*` type alias is its TypeScript inferred type. Validation entrypoints
 * use the `.parse()` / `.safeParse()` methods on the schemas.
 */

import { z } from "zod";

// ---------------------------------------------------------------------------
// Binding & metadata
// ---------------------------------------------------------------------------

export const BindingSchema = z
  .object({
    type: z.literal("principal_agent").default("principal_agent"),
    principal_id: z.string(),
    agent_id: z.string(),
  })
  .strict();
export type Binding = z.infer<typeof BindingSchema>;

export const PrincipalSchema = z
  .object({
    type: z.enum(["human", "organization", "agent"]).default("human"),
    id: z.string(),
    role_summary: z.string(),
  })
  .strict();
export type Principal = z.infer<typeof PrincipalSchema>;

export const IssuerSchema = z
  .object({
    type: z.enum(["human", "organization", "agent", "service"]).default("human"),
    id: z.string(),
    relationship_to_principal: z.string().default("self"),
  })
  .strict();
export type Issuer = z.infer<typeof IssuerSchema>;

export const AgentOperatorSchema = z
  .object({
    type: z.enum(["service", "human", "organization"]).default("service"),
    id: z.string(),
    agent_card_url: z.string().nullable().default(null),
  })
  .strict();
export type AgentOperator = z.infer<typeof AgentOperatorSchema>;

// ---------------------------------------------------------------------------
// Visibility, summary, clauses
// ---------------------------------------------------------------------------

export const VisibilitySchema = z
  .object({
    charter: z.literal("public").default("public"),
    raw_principal_context: z.literal("private").default("private"),
    // Protocol invariant #5: only two literals accepted today
    // ("not_supported_in_v0" for pre-ADR-011 Charters, "redaction_v1"
    // for ADR-011 path 1). Future paths extend this enum behind a new
    // ADR — never silently.
    private_clauses: z
      .enum(["not_supported_in_v0", "redaction_v1"])
      .default("not_supported_in_v0"),
  })
  .strict();
export type Visibility = z.infer<typeof VisibilitySchema>;

export const SummarySchema = z
  .object({
    plain_language: z.string(),
  })
  .strict();
export type Summary = z.infer<typeof SummarySchema>;

export const PrivateFieldRefSchema = z
  .object({
    span_start: z.number().int().nonnegative(),
    span_end: z.number().int().nonnegative(),
    disclosure_hash: z.string(), // "sha256:<hex>"
  })
  .strict();
export type PrivateFieldRef = z.infer<typeof PrivateFieldRefSchema>;

export const ClauseSchema = z
  .object({
    id: z.string(),
    type: z.enum([
      "scope",
      "out_of_scope",
      "approval_required",
      "operational_limit",
      "style",
      "data_handling",
    ]),
    text: z.string(),
    // pre-ADR-011 Charters omit private_fields; ADR-011 path 1 Charters
    // carry an array. null is treated the same as absent (see canonical.ts).
    private_fields: z.array(PrivateFieldRefSchema).nullable().default(null),
  })
  .strict();
export type Clause = z.infer<typeof ClauseSchema>;

// ---------------------------------------------------------------------------
// Decision schema (verdict + matched clauses)
// ---------------------------------------------------------------------------

export const MatchedClauseSchema = z
  .object({
    id: z.string(),
    local_decision: z.enum(["allow", "needs_approval", "incompatible"]),
    applied: z.boolean(),
    confidence: z.number().min(0).max(1),
    reason: z.string(),
    source_charter_id: z.string().nullable().default(null),
  })
  .strict();
export type MatchedClause = z.infer<typeof MatchedClauseSchema>;

export const VerdictSchema = z
  .object({
    decision: z.enum(["allow", "needs_approval", "incompatible"]),
    matched_clauses: z.array(MatchedClauseSchema).default([]),
    reason: z.string(),
    rewrite_available: z.boolean().default(false),
  })
  .strict();
export type Verdict = z.infer<typeof VerdictSchema>;

// ---------------------------------------------------------------------------
// Lifecycle & provenance
// ---------------------------------------------------------------------------

export const LifecycleSchema = z
  .object({
    // ISO-8601 strings; we don't auto-coerce to Date so byte-for-byte
    // equivalence with the Python serialization stays under our control.
    issued_at: z.string(),
    valid_until: z.string(),
    status: z.enum(["active", "expired", "revoked", "superseded"]).default("active"),
    revoked_at: z.string().nullable().default(null),
    replaces: z.string().nullable().default(null),
    replaced_by: z.string().nullable().default(null),
  })
  .strict();
export type Lifecycle = z.infer<typeof LifecycleSchema>;

export const SourceCommitmentSchema = z
  .object({
    type: z.string(),
    description: z.string(),
    content_hash: z.string(),
  })
  .strict();
export type SourceCommitment = z.infer<typeof SourceCommitmentSchema>;

export const ProvenanceSchema = z
  .object({
    issuer_public_key: z.string(), // "ed25519:<base64>"
    issuer_signature: z.string().default(""), // "ed25519:<base64>" once signed
    issuer_kid: z.string().nullable().default(null),
    transparency_log_id: z.number().int().nullable().default(null),
    source_commitments: z.array(SourceCommitmentSchema).default([]),
    generated_at: z.string(),
  })
  .strict();
export type Provenance = z.infer<typeof ProvenanceSchema>;

// ---------------------------------------------------------------------------
// Chain (parent reference + attenuation proof)
// ---------------------------------------------------------------------------

export const SemanticCheckResultSchema = z
  .object({
    matches_subset: z.boolean(),
    reason: z.string(),
    graded_at: z.string(),
  })
  .strict();
export type SemanticCheckResult = z.infer<typeof SemanticCheckResultSchema>;

export const AttenuationProofSchema = z
  .object({
    parent_charter_id: z.string(),
    attenuates: z.record(z.array(z.string())).default({}),
    semantic_check_cache: z.record(SemanticCheckResultSchema).default({}),
  })
  .strict();
export type AttenuationProof = z.infer<typeof AttenuationProofSchema>;

// ---------------------------------------------------------------------------
// Charter (top-level)
// ---------------------------------------------------------------------------

export const DecisionSchemaDocSchema = z
  .object({
    decision: z.string().default("allow | needs_approval | incompatible"),
    matched_clauses: z
      .string()
      .default("[{id, local_decision, applied, confidence in [0,1], reason}]"),
    reason: z.string().default("string -- short summary referencing applied clauses"),
    rewrite_available: z
      .string()
      .default("bool -- whether propose_within_scope might help"),
  })
  .strict();
export type DecisionSchemaDoc = z.infer<typeof DecisionSchemaDocSchema>;

export const CharterSchema = z
  .object({
    version: z.string().default("0.1"),
    charter_id: z.string(),
    binding: BindingSchema,
    principal: PrincipalSchema,
    issuer: IssuerSchema,
    agent_operator: AgentOperatorSchema,
    principal_chain: z.array(z.string()).default([]),
    visibility: VisibilitySchema.default({
      charter: "public",
      raw_principal_context: "private",
      private_clauses: "not_supported_in_v0",
    }),
    summary: SummarySchema,
    clauses: z.array(ClauseSchema),
    decision_schema: DecisionSchemaDocSchema.default({
      decision: "allow | needs_approval | incompatible",
      matched_clauses:
        "[{id, local_decision, applied, confidence in [0,1], reason}]",
      reason: "string -- short summary referencing applied clauses",
      rewrite_available: "bool -- whether propose_within_scope might help",
    }),
    lifecycle: LifecycleSchema,
    provenance: ProvenanceSchema,
    parent_charter_url: z.string().nullable().default(null),
    attenuation_proof: AttenuationProofSchema.nullable().default(null),
  })
  .strict();
export type Charter = z.infer<typeof CharterSchema>;

// ---------------------------------------------------------------------------
// Disclosure (privacy / SD-JWT path 1)
// ---------------------------------------------------------------------------

export const DisclosureSchema = z
  .object({
    disclosure_id: z.string(),
    span_value: z.string(),
    salt_hex: z.string(),
    disclosure_hash: z
      .string()
      .regex(/^sha256:[0-9a-f]{64}$/, "disclosure_hash must be sha256:<64-hex>"),
  })
  .strict();
export type Disclosure = z.infer<typeof DisclosureSchema>;

// ---------------------------------------------------------------------------
// Convenience: validate a Charter from arbitrary input
// ---------------------------------------------------------------------------

/**
 * Strict-validate a Charter-shaped object. Throws ZodError on any
 * extra / missing / wrong-typed field — equivalent to
 * `Charter.model_validate(...)` in Python.
 */
export function parseCharter(input: unknown): Charter {
  return CharterSchema.parse(input);
}
