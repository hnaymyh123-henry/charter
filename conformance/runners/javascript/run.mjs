/*
 * Charter conformance — JavaScript skeleton runner.
 *
 * This is a deliberate skeleton. It demonstrates that the vector format is
 * implementable in JavaScript using only Node.js stdlib (`fs`, `path`,
 * `crypto`, `url`) and exercises ONE vector end-to-end so the framework
 * shape is proven. The other ~43 vectors are intentionally TODO — full
 * coverage waits for the JS SDK (Issue B1.2) which will provide the
 * Charter API surface that the runner can dispatch into.
 *
 * Run with:
 *     cd conformance/runners/javascript
 *     node run.mjs
 *
 * Exit 0 iff every implemented vector passes; non-zero otherwise.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const __dirname = dirname(fileURLToPath(import.meta.url));
const VECTORS_DIR = join(__dirname, "..", "..", "vectors");

const GENESIS_PREV_HASH = "sha256:" + "0".repeat(64);

// ---------------------------------------------------------------------------
// Vector discovery
// ---------------------------------------------------------------------------

function walkVectors(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      out.push(...walkVectors(full));
    } else if (entry.endsWith(".json") && entry !== "generation_metadata.json") {
      out.push(full);
    }
  }
  return out;
}

function validateSchema(path, vector) {
  const required = ["name", "spec_section", "input", "expected_output", "expected_error"];
  for (const k of required) {
    if (!Object.prototype.hasOwnProperty.call(vector, k)) {
      throw new Error(`vector ${path} missing required field: ${k}`);
    }
  }
  if (vector.expected_output !== null && vector.expected_error !== null) {
    throw new Error(
      `vector ${path} declares both expected_output and expected_error; exactly one must be set`,
    );
  }
  if (
    typeof vector.input !== "object" ||
    vector.input === null ||
    !("operation" in vector.input)
  ) {
    throw new Error(`vector ${path} has no input.operation; cannot dispatch`);
  }
}

// ---------------------------------------------------------------------------
// Operation handlers — only the SPEC §8 transparency log walk is implemented
// here as a proof of cross-language portability. Everything else is TODO.
// ---------------------------------------------------------------------------

function canonicalJsonBytes(obj) {
  // SPEC §1.3 / §8.2 canonical JSON: sorted keys, no whitespace.
  function sortKeys(value) {
    if (Array.isArray(value)) {
      return value.map(sortKeys);
    }
    if (value !== null && typeof value === "object") {
      const sorted = {};
      for (const key of Object.keys(value).sort()) {
        sorted[key] = sortKeys(value[key]);
      }
      return sorted;
    }
    return value;
  }
  return Buffer.from(JSON.stringify(sortKeys(obj)), "utf-8");
}

function opVerifyLogChain(input) {
  // SPEC §8 — Walk a list of transparency log entries verifying SHA-256
  // chain integrity. This is the cleanest "no Charter API needed" op so
  // it's the demo we implement here.
  const entries = input.entries;
  if (!entries || entries.length === 0) {
    return { ok: true, entries: 0, head_hash: GENESIS_PREV_HASH };
  }
  let expectedPrev = GENESIS_PREV_HASH;
  for (const entry of entries) {
    if (entry.prev_hash !== expectedPrev) {
      return { ok: false, broken_at_seq: entry.seq };
    }
    const withoutHash = { ...entry };
    delete withoutHash.entry_hash;
    const recomputed =
      "sha256:" +
      createHash("sha256").update(canonicalJsonBytes(withoutHash)).digest("hex");
    if (recomputed !== entry.entry_hash) {
      return { ok: false, broken_at_seq: entry.seq };
    }
    expectedPrev = entry.entry_hash;
  }
  return {
    ok: true,
    entries: entries.length,
    head_hash: entries[entries.length - 1].entry_hash,
  };
}

const DISPATCH = {
  verify_log_chain: opVerifyLogChain,
  // TODO(B1.2): implement the other ~16 operations once the JS Charter SDK
  // exposes the required APIs (canonical_bytes, sign, verify, aggregate,
  // verify_chain_strict, lifecycle_status, public_key_to_jwk,
  // kid_for_public_key, pin_fingerprint, redact_clause, verify_disclosure,
  // and the canonical_bytes_* introspection variants).
};

function isTodo(op) {
  return !Object.prototype.hasOwnProperty.call(DISPATCH, op);
}

// Deep-equality good enough for our vectors (no Date / Map / Set / cycles).
function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  if (typeof a === "object") {
    const ak = Object.keys(a).sort();
    const bk = Object.keys(b).sort();
    if (ak.length !== bk.length) return false;
    if (!ak.every((k, i) => k === bk[i])) return false;
    return ak.every((k) => deepEqual(a[k], b[k]));
  }
  return false;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function runOne(path) {
  const start = process.hrtime.bigint();
  let vector;
  try {
    vector = JSON.parse(readFileSync(path, "utf-8"));
    validateSchema(path, vector);
  } catch (e) {
    return { path, name: path, passed: false, todo: false, reason: e.message };
  }
  const op = vector.input.operation;
  if (isTodo(op)) {
    return { path, name: vector.name, passed: true, todo: true, reason: null };
  }
  try {
    const actual = DISPATCH[op](vector.input);
    if (!deepEqual(actual, vector.expected_output)) {
      return {
        path,
        name: vector.name,
        passed: false,
        todo: false,
        reason:
          "output mismatch\n  expected: " +
          JSON.stringify(vector.expected_output) +
          "\n  actual:   " +
          JSON.stringify(actual),
      };
    }
    const durMs = Number(process.hrtime.bigint() - start) / 1e6;
    return { path, name: vector.name, passed: true, todo: false, reason: null, durMs };
  } catch (e) {
    return { path, name: vector.name, passed: false, todo: false, reason: e.message };
  }
}

function main() {
  let paths;
  try {
    paths = walkVectors(VECTORS_DIR).sort();
  } catch (e) {
    console.error(`No vectors found under ${VECTORS_DIR}: ${e.message}`);
    process.exit(2);
  }

  let implemented = 0;
  let passed = 0;
  let failed = 0;
  let todo = 0;
  const failures = [];

  for (const p of paths) {
    const result = runOne(p);
    const rel = relative(VECTORS_DIR, p);
    if (result.todo) {
      todo += 1;
      // Don't print TODOs by default — there are many.
    } else if (result.passed) {
      implemented += 1;
      passed += 1;
      console.log(`[PASS] ${rel}  (${(result.durMs || 0).toFixed(1)}ms)`);
    } else {
      implemented += 1;
      failed += 1;
      failures.push({ rel, reason: result.reason });
      console.log(`[FAIL] ${rel}`);
      for (const line of result.reason.split("\n")) {
        console.log(`       ${line}`);
      }
    }
  }

  console.log("");
  console.log(
    `JS skeleton runner: ${passed}/${implemented} implemented vectors passed; ` +
      `${todo} TODO (await B1.2 JS SDK)`,
  );
  if (failed === 0 && implemented > 0) {
    console.log(
      "Framework proof: vector schema parses + canonical JSON hashing matches the Python runner.",
    );
  }
  process.exit(failed === 0 ? 0 : 1);
}

main();
