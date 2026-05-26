/**
 * Ed25519 sign / verify for Self-Attesting Charters (SPEC.md §2).
 *
 * The protocol is fixed to Ed25519 (ADR-002). We refuse any signature
 * whose `provenance.issuer_signature` doesn't start with `ed25519:`
 * exactly the way the Python implementation does.
 *
 * Backend: `@noble/ed25519` — pure JS, audited, available in browsers
 * and Node. We deliberately do NOT touch `node:crypto`'s Ed25519
 * support because (a) we want one cross-runtime implementation and
 * (b) using `node:crypto` would invite "and what about RSA?" creep,
 * which ADR-002 explicitly forbids.
 *
 * The `@noble/ed25519@^2` API is async by default (it needs a SHA-512
 * implementation injected for synchronous paths). We expose async
 * `sign`/`verify` helpers; the rest of the SDK is sync.
 */

import { canonicalBytes } from "./canonical.js";
import type { Charter } from "./schema.js";

import * as ed from "@noble/ed25519";

// @noble/ed25519 v2 ships an async SHA-512 implementation (`etc.sha512Async`)
// out of the box via WebCrypto, available natively in Node 20+ (which our
// `engines.node` pins). The async sign / verify paths therefore work
// without any explicit initialization here.

// ---------------------------------------------------------------------------
// Base64 helpers (Node `Buffer` would work, but we keep this runtime-neutral)
// ---------------------------------------------------------------------------

function base64ToBytes(s: string): Uint8Array {
  // atob exists in modern Node. Fall back to Buffer only if missing.
  if (typeof atob === "function") {
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
      out[i] = bin.charCodeAt(i);
    }
    return out;
  }
  return new Uint8Array(Buffer.from(s, "base64"));
}

function bytesToBase64(bytes: Uint8Array): string {
  if (typeof btoa === "function") {
    let bin = "";
    for (let i = 0; i < bytes.length; i++) {
      bin += String.fromCharCode(bytes[i] as number);
    }
    return btoa(bin);
  }
  return Buffer.from(bytes).toString("base64");
}

// ---------------------------------------------------------------------------
// Key codec
// ---------------------------------------------------------------------------

/**
 * Parse `ed25519:<base64>` into raw 32-byte public key bytes.
 * Throws on any other prefix — non-Ed25519 keys are unsupported (ADR-002).
 */
export function publicKeyFromString(s: string): Uint8Array {
  if (!s.startsWith("ed25519:")) {
    throw new Error(
      `Expected 'ed25519:<base64>' prefix, got ${JSON.stringify(s.slice(0, 32))}`,
    );
  }
  return base64ToBytes(s.slice("ed25519:".length));
}

/**
 * Render raw 32-byte public key bytes as `ed25519:<base64>`.
 */
export function publicKeyToString(raw: Uint8Array): string {
  return `ed25519:${bytesToBase64(raw)}`;
}

/**
 * Derive a public key from a 32-byte private-key seed.
 * Convenience for tests + the conformance `sign` operation, which
 * receives a fixed seed via `private_key_seed_hex`.
 */
export async function publicKeyFromSeed(seed: Uint8Array): Promise<Uint8Array> {
  if (seed.length !== 32) {
    throw new Error(`Ed25519 seed must be 32 bytes, got ${seed.length}`);
  }
  return ed.getPublicKeyAsync(seed);
}

// ---------------------------------------------------------------------------
// Signing
// ---------------------------------------------------------------------------

/**
 * Sign a Charter and return a new Charter with the signature populated.
 *
 * Side effects on the returned Charter:
 *   - `provenance.issuer_signature` is set to `ed25519:<base64>`.
 *   - `provenance.issuer_kid` is filled in from the embedded public key
 *     (so the signed bytes commit to the kid — verifiers can detect
 *     swap attempts).
 *
 * Unlike Python's `charter.signing.sign_charter`, this does NOT append
 * to a transparency log — that's a server-side concern and not part of
 * `@charter/core` (see Issue #50 scope). Callers who want log-append
 * behavior should run `verifyLogChain` separately when consuming a log
 * stream from the server.
 *
 * The Charter input is NOT mutated; we return a clone.
 */
export async function signCharter(
  charter: Charter,
  privateKeySeed: Uint8Array,
): Promise<Charter> {
  if (privateKeySeed.length !== 32) {
    throw new Error(`Ed25519 seed must be 32 bytes, got ${privateKeySeed.length}`);
  }
  // Clone so we don't mutate caller state.
  const out = JSON.parse(JSON.stringify(charter)) as Charter;

  // Populate kid first so it's covered by the signature.
  if (out.provenance.issuer_kid === null || out.provenance.issuer_kid === undefined) {
    const { kidForPublicKey } = await import("./jwks.js");
    out.provenance.issuer_kid = kidForPublicKey(out.provenance.issuer_public_key);
  }

  // Clear signature for canonical bytes computation — `canonicalBytes`
  // does this defensively too, but doing it on the clone keeps the
  // returned object in a consistent state.
  out.provenance.issuer_signature = "";

  const payload = canonicalBytes(out);
  const sigBytes = await ed.signAsync(payload, privateKeySeed);
  out.provenance.issuer_signature = `ed25519:${bytesToBase64(sigBytes)}`;
  return out;
}

// ---------------------------------------------------------------------------
// Verification
// ---------------------------------------------------------------------------

/**
 * Verify a Charter's `issuer_signature` against its embedded public key.
 *
 * Returns `true` iff:
 *   - `issuer_signature` parses as `ed25519:<base64>`,
 *   - the decoded signature is exactly 64 bytes,
 *   - the embedded public key is a valid Ed25519 key, and
 *   - the signature verifies against `canonicalBytes(charter)` under
 *     that public key.
 *
 * MUST NOT check expiry, revocation, lifecycle, JWKS, or pins. Those
 * are higher-level concerns (SPEC.md §11 fetch+verify ordering).
 *
 * Per ADR-002, ANY non-`ed25519:` prefix on the signature triggers a
 * `false` return — we never silently accept a foreign algorithm.
 */
export async function verifyCharter(charter: Charter): Promise<boolean> {
  const sigStr = charter.provenance.issuer_signature;
  if (typeof sigStr !== "string" || !sigStr.startsWith("ed25519:")) {
    return false;
  }
  try {
    const signature = base64ToBytes(sigStr.slice("ed25519:".length));
    if (signature.length !== 64) {
      return false;
    }
    const publicKey = publicKeyFromString(charter.provenance.issuer_public_key);
    const payload = canonicalBytes(charter);
    return await ed.verifyAsync(signature, payload, publicKey);
  } catch {
    return false;
  }
}
