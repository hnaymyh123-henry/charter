# Charter Conformance Specification (v0.1)

> The contract another implementation MUST satisfy to call itself "a Charter
> implementation". Numbered sections are stable anchors — every JSON test
> vector under `conformance/vectors/` references one of them via its
> `spec_section` field.
>
> Source of truth for the protocol itself is [`PRODUCT.md`](../PRODUCT.md)
> and [`docs/decisions.md`](../docs/decisions.md). This document narrows
> those into machine-checkable rules.

**Spec version:** `0.1` (initial).

Any change to a numbered rule's observable behavior is a **breaking change**:
bump this version, regenerate vectors, communicate downstream.

---

## §1 — Canonical bytes for signing  *(invariant #1, ADR-003, ADR-011)*

### §1.1 Field exclusions
`Charter` is serialized for signing with the following fields **cleared**
(not merely set to empty string — actively replaced before serialization):

- `provenance.issuer_signature` → set to `""`.
- `provenance.transparency_log_id` → set to `null`.

### §1.2 None elision (backward compatibility)
For every entry in `clauses[*]`, if `private_fields` is `None` / `null`, the
**entire key** is removed from the canonical payload — not serialized as
`"private_fields": null`. This preserves byte-identical canonical bytes for
Charters that predate ADR-011.

### §1.3 JSON shape
- UTF-8 encoded.
- Object keys sorted lexicographically at every nesting level.
- No whitespace between tokens (`json.dumps(..., separators=(",", ":"))`).
- Datetimes are written in the same ISO-8601 form Pydantic v2 emits with
  `model_dump(mode="json")` — vector `expected_output.canonical_bytes_sha256`
  pins the exact byte sequence.

### §1.4 Conformance test
Vectors under `vectors/sign/canonical_bytes_*.json` ship the input `Charter`
JSON + the expected SHA-256 of the canonical bytes. An implementation passes
if recomputed SHA-256 matches.

---

## §2 — Ed25519 sign / verify  *(invariant #1, ADR-002)*

### §2.1 Algorithm
- Signing key: Ed25519 (RFC 8032).
- Signature format on the wire: `ed25519:<base64-of-64-byte-signature>`.
- Public key format on the wire: `ed25519:<base64-of-32-byte-raw-public-key>`.
- **Reject** any other prefix (`rsa:`, `secp:`, `ecdsa:`, etc.) — vectors
  under `vectors/verify/reject_non_ed25519_*.json` exercise this.

### §2.2 Verification scope
`verify_charter` returns `True` iff the Ed25519 signature decoded from
`provenance.issuer_signature` validates against the canonical bytes (§1)
under the public key in `provenance.issuer_public_key`.

The verifier MUST NOT check expiry, revocation, lifecycle, JWKS, or pins
during this primitive — those are higher-level concerns. (See §5.)

### §2.3 Tamper detection
Any change to a covered field (anything except the two §1.1 exclusions)
invalidates the signature. Vectors `vectors/verify/tampered_*.json` cover:
clause text edit, principal_id swap, lifecycle date change, summary edit.

---

## §3 — TYPE_TO_DECISION  *(invariant #2, ADR-004)*

The six clause types map deterministically to local decisions. **Exactly
this mapping; no other values; no implementation override.**

| `Clause.type`         | Local decision    |
|-----------------------|-------------------|
| `scope`               | `allow`           |
| `out_of_scope`        | `incompatible`    |
| `approval_required`   | `needs_approval`  |
| `operational_limit`   | `needs_approval`  |
| `style`               | `allow`           |
| `data_handling`       | `needs_approval`  |

Vectors `vectors/aggregate/type_to_decision_*.json` exercise each row.

---

## §4 — Aggregate verdict  *(invariant #3, ADR-005)*

### §4.1 Precedence
`incompatible` > `needs_approval` > `allow`. The aggregate decision is the
maximum of the per-clause local decisions under this ordering. Strictly
monotonic — adding a stricter decision never relaxes the aggregate.

### §4.2 Closed-world fallback
When `local_decisions` is empty (zero clauses matched, or all dropped for
low confidence), aggregate decision is `needs_approval`. **Not** `allow`.

### §4.3 Conformance test
Vectors `vectors/aggregate/aggregate_*.json` ship lists of decisions and
the expected aggregate.

---

## §5 — Lifecycle state machine  *(invariant)*

`Lifecycle.status` is one of: `active`, `expired`, `revoked`, `superseded`.
Transitions are issuer-driven; the protocol does not enforce a state
machine at verification time. However, the **policy layer** treats the
last three as non-allowable for new delegations:

- `active`: usable.
- `expired`: `valid_until` is in the past. Higher-level policy MUST treat
  as `needs_approval` or `incompatible`.
- `revoked`: `revoked_at` populated. Higher-level policy MUST treat as
  `incompatible`.
- `superseded`: `replaced_by` populated. Higher-level policy SHOULD
  redirect to the successor Charter.

Vectors `vectors/lifecycle/*.json` exercise each status and its expected
policy classification.

---

## §6 — Chain verification  *(invariant, ADR-010)*

### §6.1 Strict (string-based, mandatory)
Every conformant implementation MUST support `verify_chain(child, parent,
mode="strict")`. Rules (verbatim from `charter.chain._verify_chain_strict`):

1. If `child.attenuation_proof.parent_charter_id` is set, it MUST equal
   `parent.charter_id`. Else chain rejected.
2. For every `out_of_scope` clause in parent, some `out_of_scope` clause in
   child covers it. Coverage = text equality OR parent's stripped text is a
   substring of child's stripped text.
3. Same for every `approval_required` clause.
4. For every `scope` clause in child, its stripped text MUST equal some
   `scope` clause in parent (exact equality; superstring NOT allowed —
   child cannot widen scope).
5. `operational_limit`, `style`, `data_handling` are NOT checked by §6.1.

### §6.2 Semantic (LLM-based, optional)
`verify_chain(..., mode="semantic")` and `mode="auto"` exist for tolerance
of reworded clauses. Conformant implementations MAY skip semantic mode
(it requires an LLM client); when implemented, results MUST be cached in
`child.attenuation_proof.semantic_check_cache` keyed by
`f"{parent.charter_id}@{parent.lifecycle.issued_at.isoformat()}"`, so a
re-sign of the parent invalidates every cached verdict against it.

### §6.3 Cycle / depth bounds
Chain walking MUST detect cycles (a child whose ancestor chain loops back
to itself) and MUST cap traversal at a finite depth (Charter reference
implementation uses 8). Vectors `vectors/chain/cycle_*.json` and
`vectors/chain/depth_bound_*.json` exercise both.

---

## §7 — JWKS (RFC 7517 + `iss` extension)  *(ADR-007)*

### §7.1 JWK shape
Each Ed25519 public key is rendered as:

```json
{
  "kty": "OKP",
  "crv": "Ed25519",
  "kid": "<16-hex-char-prefix-of-sha256(raw_public_key)>",
  "x":   "<base64url-no-padding-of-raw-public-key>",
  "use": "sig",
  "alg": "EdDSA"
}
```

### §7.2 `kid` derivation
`kid = sha256(raw_public_key_bytes).hex()[:16]` (lowercase hex, first 16
chars). Stable across processes and machines.

### §7.3 JWKS document
`/.well-known/jwks.json` returns `{"keys": [<jwk>, ...], "iss": "<origin>"}`.
The `iss` field is a Charter extension to RFC 7517 used by the v0.8 trust
model to cross-check that the JWKS belongs to the same origin as the
Charter URL.

### §7.4 Cross-check
A verifier given a Charter MUST be able to locate the Charter's
`provenance.issuer_public_key` somewhere inside the JWKS — by `kid` if
`provenance.issuer_kid` is set, by `x` value otherwise.

Vectors `vectors/jwks/*.json` exercise jwk derivation and cross-check.

---

## §8 — Transparency log  *(ADR-007)*

### §8.1 Append-only, SHA-256 chained
The log is JSON Lines. Each entry:

```json
{
  "seq": <int, monotonically increasing from 1>,
  "charter_id": "<string>",
  "binding": {"principal_id": "<string>", "agent_id": "<string>"},
  "issuer_kid": "<string-or-null>",
  "issuer_signature": "ed25519:<base64>",
  "appended_at": "<iso-8601-utc>",
  "prev_hash": "sha256:<hex>",
  "entry_hash": "sha256:<hex>"
}
```

### §8.2 Hash derivation
- `prev_hash` of seq 1 is the genesis value: `"sha256:" + "0" * 64`.
- `prev_hash` of seq N (N>1) equals `entry_hash` of seq N-1.
- `entry_hash` = `sha256(canonical_json(entry_without_entry_hash))`,
  serialized with the same convention as §1.3 (sorted keys, no whitespace).

### §8.3 Verification
Walking the log MUST verify both that every `prev_hash` matches the
previous `entry_hash` AND that every `entry_hash` matches a fresh hash
of its own fields. Any mismatch fails the chain.

Vectors `vectors/transparency/*.json` exercise valid chains, tampered
prev_hash, and tampered entry payload.

---

## §9 — Pinning  *(ADR-007)*

### §9.1 Fingerprint format
`fingerprint = "sha256:" + sha256(raw_public_key_bytes).hexdigest()`. The
**full** digest (no truncation) — pinning is the layer that must resist
adversarial key choice.

### §9.2 Pin file format
JSON object keyed by principal identifier:

```json
{
  "<principal_id>": {
    "fingerprint":   "sha256:<full-hex>",
    "first_seen":    "<iso-8601-utc>",
    "last_verified": "<iso-8601-utc>"
  }
}
```

First fetch establishes the pin (TOFU). Subsequent fetches compare the
current key's fingerprint to the stored one; mismatch → `incompatible`.

Vectors `vectors/jwks/pin_*.json` cover first-pin + mismatch.

---

## §10 — Selective disclosure (SD-JWT path 1)  *(invariant #5, ADR-011 path 1)*

### §10.1 Per-span redaction
Sensitive substrings of `Clause.text` are replaced by
`[REDACTED:<hash-prefix>]` placeholders where `<hash-prefix>` is the first
8 lowercase-hex chars of the `disclosure_hash` (§10.2).

### §10.2 Hash format
`disclosure_hash = "sha256:" + sha256(salt_bytes || value.encode("utf-8")).hexdigest()`.
Salt is 16 random bytes per span (deterministic only in test contexts).

### §10.3 Canonical bytes participation
**The plaintext span value NEVER enters canonical bytes.** Only the
`PrivateFieldRef` entry (containing `span_start`, `span_end`,
`disclosure_hash`) does, and only when `private_fields` is non-None
(see §1.2).

### §10.4 Disclosure verification
Given a `Disclosure` record `{disclosure_id, span_value, salt_hex,
disclosure_hash}` and a claimed hash from the matching `PrivateFieldRef`:

- Decode `salt_hex` as hex → `salt_bytes`.
- Recompute `sha256:<hex>` over `salt_bytes || span_value.encode("utf-8")`.
- Return `True` iff recomputed value equals BOTH `claimed_hash` and
  `disclosure.disclosure_hash`.

Vectors `vectors/privacy/*.json` cover redaction roundtrip, disclosure
verification, and the §10.3 "plaintext never in signature" invariant.

---

## §11 — Fetch + verify ordering  *(invariant #6)*

When fetching a Charter and deciding whether to trust it, the verifier
MUST run these checks in this order, short-circuiting on first failure:

1. **Signature** (§2.2) — invalid signature is `incompatible`, full stop.
2. **JWKS cross-check** (§7.4) — public key not findable in JWKS is
   `incompatible`.
3. **Pin check** (§9) — fingerprint mismatch against stored pin is
   `incompatible`.
4. **Lifecycle** (§5) — expired / revoked → policy decides.

Reordering changes the failure mode an attacker sees; conformant
implementations MUST follow this order. (This is a sequencing rule, not
a primitive — there is no single vector for §11; the rule is observed
across §2/§7/§9/§5 vectors run in sequence by a host.)

---

## Appendix A — Vector JSON schema

Every file under `vectors/**/*.json` MUST conform to:

```json
{
  "name": "human-readable name",
  "spec_section": "SPEC.md#section-anchor",
  "input": {...},
  "expected_output": {...} | null,
  "expected_error": null | "ErrorTypeName"
}
```

Exactly one of `expected_output` (non-null) or `expected_error` (non-null
string) is set per vector. Runners that encounter both `expected_output`
non-null AND `expected_error` non-null MUST treat the vector as malformed
and report it as an error rather than passing.

## Appendix B — Versioning

Spec version follows the Charter project version. Vectors are generated
from a fixed Charter Python implementation revision; the generator script
records the revision in `vectors/generation_metadata.json` so a future
reader can tell when vectors were last refreshed.

A bump from `0.X` → `0.(X+1)` indicates **non-breaking** additions
(new sections, new vectors). A bump from `0.X` → `1.0` (or `1.X` →
`2.0`) indicates breaking changes — implementations MUST opt in and
update.
