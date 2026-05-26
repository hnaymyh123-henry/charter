/**
 * JWK / kid helpers for the v0.8 trust model (SPEC.md §7).
 *
 * Mirrors `charter.signing.kid_for_public_key` and `public_key_to_jwk`
 * from Python. The `kid` is stable across processes and machines —
 * same Ed25519 key → same kid byte-for-byte.
 */

import { createHash } from "node:crypto";

import { publicKeyFromString } from "./signing.js";

/**
 * Stable JWKS `kid` for an Ed25519 public key.
 *
 * Format: first 16 lowercase-hex chars of `sha256(raw_public_key)`.
 * Matches Python `charter.signing.kid_for_public_key` exactly.
 */
export function kidForPublicKey(publicKeyStr: string): string {
  const raw = publicKeyFromString(publicKeyStr);
  return createHash("sha256").update(raw).digest("hex").slice(0, 16);
}

/**
 * Render an Ed25519 public key as an RFC 7517 JWK object.
 *
 * Output keys: `kty="OKP"`, `crv="Ed25519"`, `x=<base64url(raw)>`,
 * `kid=<kidForPublicKey>`, `use="sig"`, `alg="EdDSA"`.
 *
 * If `kid` is supplied, it overrides the derived value — handy for
 * tests that want a known kid, never used in production code.
 */
export function publicKeyToJwk(
  publicKeyStr: string,
  options?: { kid?: string },
): Record<string, string> {
  const raw = publicKeyFromString(publicKeyStr);
  // base64url with no padding, per RFC 7515 §2.
  const xB64Url = Buffer.from(raw)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

  const kid = options?.kid ?? kidForPublicKey(publicKeyStr);
  return {
    kty: "OKP",
    crv: "Ed25519",
    kid,
    x: xB64Url,
    use: "sig",
    alg: "EdDSA",
  };
}
