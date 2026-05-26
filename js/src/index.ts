/**
 * @charter/core — Charter protocol SDK for JavaScript / TypeScript.
 *
 * Cross-language byte-equivalent with the Python reference at
 * github.com/hnaymyh123-henry/charter (see `charter/` package).
 *
 * Scope (Issue #50):
 *   - schema (zod runtime + TypeScript inferred types)
 *   - canonical bytes
 *   - Ed25519 sign / verify
 *   - aggregate verdict
 *   - strict chain verification
 *   - transparency log chain verification
 *   - JWK + kid derivation
 *   - pin fingerprint
 *   - lifecycle policy classifier
 *   - SD-JWT path 1 privacy primitives
 *
 * NOT in scope (deferred to `@charter/server`):
 *   - HTTP server
 *   - MCP server
 *   - CLI
 *   - encrypted private-key storage
 *   - JWKS HTTP fetch / pin store
 *   - disclosure HTTP fetch
 *   - semantic (LLM-graded) chain mode
 */

export * from "./constants.js";
export * from "./schema.js";
export * from "./canonical.js";
export * from "./signing.js";
export * from "./aggregate.js";
export * from "./chain.js";
export * from "./jwks.js";
export * from "./pins.js";
export * from "./lifecycle.js";
export * from "./transparency.js";
export * from "./privacy.js";
