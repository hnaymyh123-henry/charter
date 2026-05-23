# 08 — Self-host `.well-known/charter/<agent_id>`

> **TL;DR.** Set `CHARTER_SELF_HOSTED_PRINCIPAL=<your_principal_id>` and
> `charter-server` flips into single-principal mode. Your Charters are
> then discoverable at the AP2 / Web Bot Auth-style location
> `https://<your_domain>/.well-known/charter/<agent_id>` instead of the
> multi-tenant `/<principal>/<agent>` path. The JWKS endpoint filters to
> the same principal, so a calling agent only ever talks to your domain
> for both verification artifacts.

## Why this guide

Two ways to publish Charters:

1. **SaaS / multi-tenant mode** (default). All Charters for all
   principals share one origin under
   `{CHARTER_URL_BASE}/{principal_id}/{agent_id}`. Fine for development,
   demos, and any setup where one operator runs the Charter service for
   many principals.

2. **Self-hosted single-principal mode.** The principal runs the server
   on their own domain. The discovery URL becomes
   `https://<their_domain>/.well-known/charter/<agent_id>` — the same
   shape Web Bot Auth uses for `signature-agent`. This is what you want
   when:

   - You don't want a third party to know which calling agents are
     looking up your Charters.
   - You want a Charter URL that's stable, brandable, and short.
   - You're going to publish a Charter URL in your AP2 mandates or
     A2A handshake and want callers to find both Charter and JWKS at
     known paths under your origin.

This guide stands up self-hosted mode end-to-end (in-process for
runnability) and shows what each endpoint returns.

## Prerequisites

- Python 3.12+, `pip install -e .`.
- For the production version: a domain you control, TLS, and an
  ingress that routes `<domain>/.well-known/charter/*` and
  `<domain>/.well-known/jwks.json` to the `charter-server` process.

## Step-by-step

### 1. Set the env var

The whole switch is one variable:

```bash
export CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com
charter-server
```

When the variable is set:

- `GET /.well-known/charter/<agent_id>` resolves Charters under
  `alice@acme.com` only.
- `GET /.well-known/jwks.json` filters its key list to keys belonging
  to `alice@acme.com`.
- Any other principal_id in a URL path returns 404 (the canonical
  `/<p>/<a>` route still exists but only matches Alice's bindings).

When unset (default), the server runs in multi-tenant mode and serves
every principal it has on disk.

### 2. Verify the discovery URL

Once the server is up, three checks:

```bash
# (a) The well-known URL returns the Charter.
curl -s http://localhost:8000/.well-known/charter/research_agent_v1 | jq .charter_id
# "charter:alice@acme.com:research_agent_v1:<DATE>"

# (b) The canonical URL still works for the same Charter.
curl -s http://localhost:8000/alice@acme.com/research_agent_v1 | jq .charter_id
# Same charter_id.

# (c) An agent the principal doesn't own returns 404.
curl -s -o /dev/null -w "%{http_code}\n" \
    http://localhost:8000/.well-known/charter/nonexistent_agent
# 404
```

The cookbook example runs all three (plus the JWKS filter check) and
asserts on each.

### 3. Verify the JWKS filter

```bash
curl -s http://localhost:8000/.well-known/jwks.json | jq '.keys | map(.iss) | unique'
# ["alice@acme.com"]
```

The cross-issuer JWKS leak (multi-tenant mode publishes *every* known
issuer's key) is exactly what self-hosted mode prevents. Production
self-hosted deployments rely on this so a caller verifying Alice's
Charter never has to inspect Bob's keys.

### 4. Production: ingress wiring

The Charter server itself is a single FastAPI app on one port. To map
to `https://<domain>/.well-known/...`, the ingress needs two routes:

- `https://<domain>/.well-known/charter/*` → `charter-server`'s port
- `https://<domain>/.well-known/jwks.json` → `charter-server`'s port

…plus the canonical `/<principal>/<agent>` if you want both URL shapes
to work (recommended for back-compat).

On fly.io (the project's reference deployment), the `fly.toml` and
Dockerfile in the repo root already expose port `CHARTER_PORT` (default
8000). You just have to:

```bash
fly secrets set CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com
fly deploy
```

and your `.well-known` URLs become `https://<your-app>.fly.dev/.well-known/...`.

For a custom domain on fly.io, follow the
[fly certificate docs](https://fly.io/docs/networking/custom-domain/)
and the same `.well-known` paths will resolve under your domain.

## Verification

Run:

```bash
python examples/cookbook/08-self-host-well-known/main.py
```

Expected (port and date will vary):

```
========================================================================
Cookbook #08 -- Self-host .well-known/charter/<agent_id>
========================================================================

Seeded Charter: alice@acme.com x research_agent_v1
Started charter-server with CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com
Origin: http://127.0.0.1:<PORT>

GET http://127.0.0.1:<PORT>/.well-known/charter/research_agent_v1
  -> HTTP 200
  -> charter_id: charter:alice@acme.com:research_agent_v1:<DATE>
  -> binding:    alice@acme.com x research_agent_v1

GET http://127.0.0.1:<PORT>/alice@acme.com/research_agent_v1
  -> HTTP 200
  -> charter_id: charter:alice@acme.com:research_agent_v1:<DATE>
  -> identical to /.well-known/charter/research_agent_v1 body

GET http://127.0.0.1:<PORT>/.well-known/charter/nonexistent_agent
  -> HTTP 404  (expected 404)

GET http://127.0.0.1:<PORT>/.well-known/jwks.json
  -> keys: 1  issuers: {'alice@acme.com'}

[OK] /.well-known/charter/<agent_id> serves only this principal's Charters,
     JWKS endpoint filters to the same principal, and unknown agents 404.
```

Asserts:

1. `/.well-known/charter/research_agent_v1` returns HTTP 200 and a
   Charter whose binding matches the configured principal.
2. The same Charter is byte-identical to the response from the
   canonical `/<principal>/<agent>` URL.
3. `/.well-known/charter/nonexistent_agent` returns HTTP 404.
4. `/.well-known/jwks.json` only lists Alice as the `iss`.

## Common pitfalls

- **Setting `CHARTER_SELF_HOSTED_PRINCIPAL` to a principal you haven't
  issued any Charter for.** The server boots fine, but every well-known
  URL 404s. Run `charter inspect` to confirm at least one binding
  exists on disk before going live.
- **Trying to expose two principals in self-hosted mode.** Self-hosted
  is *single*-principal by design. If you have two principals on one
  server, run multi-tenant mode or run two separate `charter-server`
  processes on two subdomains (`alice.example.com`, `bob.example.com`),
  each with its own `CHARTER_SELF_HOSTED_PRINCIPAL`.
- **Forgetting that JWKS also filters.** In multi-tenant mode the
  JWKS document leaks every known issuer's `(iss, kid)` pair. That's
  often fine on a shared SaaS host but a privacy leak on a personal
  domain. Self-hosted mode is what plugs the leak.
- **Hard-coding `localhost:8000` in calling agents.** When you publish
  via `.well-known`, the canonical Charter URL becomes
  `https://<domain>/.well-known/charter/<agent_id>`. Callers should
  treat that as the authoritative URL and not back-compute "well it
  came from `<domain>` so I'll fetch from `<domain>/<principal>/<agent>`".
- **Missing TLS.** `.well-known` is universally understood as
  HTTPS-only. Serving these URLs over plain HTTP gives you the syntax
  but defeats the whole verification chain (TOFU on first fetch leans on
  the HTTPS root of trust per ADR-007).

## Related guides

- [09 — Deploy a JWKS and rotate keys](09-deploy-jwks.md) — the JWKS
  endpoint that self-hosted mode filters here.
- [10 — Audit the transparency log](10-audit-transparency-log.md) — the
  `/transparency/*` endpoints work the same way regardless of mode; in
  self-hosted mode they show only this principal's history.

## What's next

- The route source is at
  [`charter/server.py`](../../charter/server.py) — search for
  `well_known_charter` and `well_known_jwks`.
- For an end-to-end walkthrough including the fly.io deploy, see
  the `Deployment (fly.io)` section in the repo
  [`README.md`](../../README.md).
- The Web Bot Auth integration (RFC 9421) is on the v0.9 roadmap;
  Charter's `.well-known/charter/...` URL is intentionally compatible
  with the discovery shape `signature-agent` headers use.
