# 09 — Deploy a JWKS and rotate keys

> **TL;DR.** `charter-server` already publishes a JWKS at
> `/.well-known/jwks.json`. To rotate, generate a new keypair, replace
> the issuer PEM on disk, reissue the Charter; the next fetch from a
> calling agent without a pin reset raises `CharterPinMismatchError`.
> The operator runs `charter pins reset <principal>` (on the calling
> side) to authorize the new key, and a fresh pin is recorded.
> Multi-step but each step is one command.

## Why this guide

Charter v0.8 layered three independent checks on top of the v0 inline
key (see
[ADR-007](../decisions.md#adr-007--self-attesting-charterv0--jwks--pin--transparency-logv08)):

1. **Inline-key signature** — the Charter's `provenance.issuer_signature`
   verifies against `provenance.issuer_public_key`.
2. **JWKS cross-check** — when the Charter carries a `kid`, the inline
   key must equal the JWKS-published key for that `kid`.
3. **Fingerprint pin** — the inline key's SHA-256 fingerprint must
   equal a previously-pinned fingerprint for this principal.

Key rotation has to update *all three* without breaking callers. This
guide walks the canonical rotation:

- Generate a new keypair.
- Replace the issuer PEM on disk → `charter-server` automatically
  publishes the new key in JWKS (the route reads from disk every call).
- Reissue every active Charter for this principal so the new
  `issuer_public_key` and `issuer_kid` are baked into the signed payload.
- Communicate to callers: "we rotated, please run `charter pins reset`."

The example does all of this in-process so you can watch the failure
state (pin mismatch) and the recovery state (fresh pin) on one terminal.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- `httpx` (already a Charter dep).

## Step-by-step

### 1. The JWKS endpoint ships out of the box

`charter-server` exposes `/.well-known/jwks.json` unconditionally:

```bash
curl -s http://localhost:8000/.well-known/jwks.json | jq
# {
#   "keys": [
#     { "kty": "OKP", "crv": "Ed25519", "x": "...", "kid": "<16-hex>",
#       "iss": "alice@acme.com" }
#   ]
# }
```

The JWKS body is generated on the fly from the PEM files in
`data/keys/`. There's no separate "jwks.json" file to maintain — the
endpoint reads disk each call.

In **multi-tenant mode** the JWKS lists every known issuer, with an
`iss` field on each entry so callers can match by `(iss, kid)`. In
**self-hosted mode** (`CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com`)
the list is filtered to one principal. See
[08 — Self-host `.well-known/charter`](08-self-host-well-known.md).

### 2. Issue a Charter (anchor the pin)

```bash
python examples/cookbook/01-write-charter-for-accountant/main.py
```

…or the offline path. The first time a calling agent fetches the
Charter, it pins the key's fingerprint into `data/pins.json`:

```
INFO  charter.pins  pin recorded  principal_id='alice@acme.com'
                                  fingerprint='sha256:fe811698...'
                                  outcome='pinned'
```

### 3. Rotate

For a planned rotation, the operator:

```bash
# (a) Generate a new keypair and replace the on-disk PEM.
python -c "
from charter.signing import generate_keypair, save_private_key
from charter.storage import key_path

private, _ = generate_keypair()
save_private_key(private, key_path('alice@acme.com'))
"

# (b) Reissue every active Charter for this principal so the new
#     public key + new kid are inside the signed payload.
charter issue profiles/alice.yaml
# (or seed_demo / your custom issue script)

# (c) Verify JWKS now publishes the new kid.
curl -s http://localhost:8000/.well-known/jwks.json | jq '.keys | map(.kid)'
```

The JWKS endpoint will reflect the new key on the very next request.
Calling agents that haven't seen the new Charter still hit the old
`/<principal>/<agent>` path with the old kid; they'll start failing
because:

1. The OLD Charter on disk got replaced by the NEW one in step (b).
2. The NEW Charter's `kid` matches the NEW JWKS entry, but its
   `issuer_public_key` doesn't match the caller's pinned fingerprint.
3. → `CharterPinMismatchError` on every caller's next fetch.

This is the **intended behavior**. The pin layer's whole job is to
make surprise rotations visible.

### 4. Reset the pin on each calling agent

Calling agents see the failure as `CharterPinMismatchError`. The
operator on each calling agent runs:

```bash
charter pins reset alice@acme.com
# Prompts for confirmation, then:
[OK] Pin dropped for alice@acme.com.
  The next fetch will establish a fresh pin.
```

Next fetch hits the TOFU branch again — pins the new fingerprint and
the Charter is verified end-to-end under the new key.

## Verification

Run the example:

```bash
python examples/cookbook/09-deploy-jwks/main.py
```

You should see (timestamps + hex digests will differ):

```
========================================================================
Cookbook #09 -- Deploy a JWKS and rotate keys
========================================================================

step 1  Issued Charter under K1.
        K1 public:  ed25519:<base64>
        K1 fingerprint: sha256:fe81...
        charter-server up at http://127.0.0.1:<PORT>

step 1  GET /.well-known/jwks.json -> 1 key(s)
          kid=fe811698661283b3  iss=alice@acme.com

step 1  fetch_and_verify ok; pin recorded: sha256:fe81...

step 2  Rotated to K2; reissued Charter.
        K2 public:  ed25519:<base64>
        K2 fingerprint: sha256:6309...

step 2  GET /.well-known/jwks.json -> kid set changed ['fe81...'] -> ['6309...']

step 3  expected refusal: CharterPinMismatchError raised.
        message: Pinned fingerprint for 'alice@acme.com' is sha256:fe81... ...

step 4  reset_pin('alice@acme.com') -> dropped old pin
        fetch_and_verify ok; new pin: sha256:6309...
        old pin == new pin? False

[OK] Rotation flow: pin catches surprise rotation; reset re-establishes trust.
```

Asserts:

- After step 1 a pin exists with the K1 fingerprint.
- After step 2 JWKS publishes K2 (kid set differs from step 1).
- Step 3 raises `CharterPinMismatchError` on re-fetch.
- After `reset_pin(...)` the next fetch succeeds and the pin matches K2.

## Common pitfalls

- **Rotating the key but not reissuing the Charter.** The on-disk
  Charter still embeds K1's `issuer_public_key` and `issuer_kid`. Now
  the signature verification fails because the *file* says K1 but the
  *file's signature* was made with K1 (still verifies) — yet the
  *current signing key* (K2) doesn't sign anything because no fresh
  Charter was issued. End state: stale Charter, callers don't notice
  the rotation until you reissue.
- **Reissuing the Charter but not rotating the key.** The new Charter
  is signed by K1, embeds K1, and the JWKS still lists K1. From the
  outside it's indistinguishable from "we didn't rotate at all." Make
  sure step (a) runs before step (b).
- **Forgetting to communicate to callers.** Callers can't tell the
  difference between "Alice rotated" and "someone is impersonating
  Alice." Both produce `CharterPinMismatchError`. The recovery
  procedure (`charter pins reset`) is intentionally one command, but
  the *decision* to run it is a human one. Tell your callers when you
  rotate.
- **Running `charter pins reset` on the issuer side.** The pin file
  lives on the *caller's* side. Resetting on the issuer's box does
  nothing useful — callers each have their own `data/pins.json`.
- **JWKS cache TTL.** The default is 300 seconds (5 min). After a
  rotation, callers will see the new JWKS within that window. For a
  production rotation, either wait out the TTL window or set
  `CHARTER_JWKS_CACHE_TTL=0` on calling agents until the rotation
  propagates.
- **Encryption passphrase mismatch.** If `CHARTER_KEY_PASSPHRASE` is
  set, both old and new PEMs must be encrypted with the same
  passphrase. Rotating the passphrase is a separate operation from
  rotating the key — do not combine them.

## Related guides

- [07 — Profile YAML best practices](07-profile-yaml-best-practices.md)
  — the source of the Charter content that gets reissued during
  rotation.
- [08 — Self-host `.well-known/charter`](08-self-host-well-known.md) —
  the JWKS filter mode that's relevant for single-principal deployments.
- [10 — Audit the transparency log](10-audit-transparency-log.md) — the
  rotation event leaves a fingerprint trail in the transparency log
  (each reissued Charter appends a new entry under the same binding
  with a new issuer_kid).

## What's next

- The JWKS route is in
  [`charter/server.py`](../../charter/server.py) (`well_known_jwks`).
- The JWKS client + cache is in
  [`charter/keys.py`](../../charter/keys.py).
- The pin module is in
  [`charter/pins.py`](../../charter/pins.py); the CLI surface is the
  `pins` group in [`charter/cli.py`](../../charter/cli.py).
- For an automated rotation playbook in production, wrap the steps
  above in a script that does (key-rotate → reissue → notify-callers
  via Slack) in one shot, then bake the script into your deployment
  pipeline.
