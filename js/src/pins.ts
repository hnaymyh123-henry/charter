/**
 * Pin fingerprints (SPEC.md §9).
 *
 * Pinning is the layer that must resist adversarial key choice, so we
 * publish the FULL SHA-256 hex digest (no truncation like JWKS kid).
 *
 * This module provides only the primitive — pin storage (TOFU files,
 * mismatch handling, etc.) lives in `@charter/server` (deferred per
 * Issue #50 scope) or in your own host integration.
 */

import { createHash } from "node:crypto";

import { publicKeyFromString } from "./signing.js";

/**
 * Compute the pin fingerprint of an `ed25519:<base64>` public key.
 *
 * Returns `sha256:<64-hex-chars>`. Same value as Python
 * `charter.pins.fingerprint_of`.
 */
export function fingerprintOf(publicKeyStr: string): string {
  const raw = publicKeyFromString(publicKeyStr);
  const hex = createHash("sha256").update(raw).digest("hex");
  return `sha256:${hex}`;
}
