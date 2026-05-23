# Charter

> The Authority layer between Agent Card (capability) and AP2 Mandate
> (per-task authorization) — a signed, queryable work contract that lets
> calling agents check "can I delegate this to that agent under this
> principal?" *before* the task runs.

Charter answers the question every existing agent protocol leaves blank:

> _"This agent acts for whom, under what continuing constraints?"_

`Agent Card` says what an agent *can do* (its résumé). `AP2 Mandate` says
what a user *authorized for this transaction*. Charter says what the agent
*may do under a given principal* (the employment contract). One agent, many
Charters, one per principal — same binary, different boundaries.

**Docs:**

- **[`PRODUCT.md`](PRODUCT.md)** — the canonical product document: what Charter is, why, how it works (protocol + schema + tools), current state, and roadmap. Start here. (Chinese mirror: [`PRODUCT.zh.md`](PRODUCT.zh.md).)
- **[`CONTEXT.md`](CONTEXT.md)** — glossary of every term used in PRODUCT.md.
- **[`ROADMAP.md`](ROADMAP.md)** — detailed iteration plans for v0.5 / v0.6 / v0.7.
- **[`AGENTS.md`](AGENTS.md)** — protocol behavior for worker agents (the 5-step Compatibility Check loop).
- **[`docs/cookbook/`](docs/cookbook/)** — scenario-driven how-to guides. Start here if you want to *do* something with Charter rather than understand the design.
- **[`docs/legacy/hackathon-design.md`](docs/legacy/hackathon-design.md)** — original hackathon design doc, kept for historical reference.

## Cookbook

Ten scenario-driven how-to guides with runnable code samples under
[`examples/cookbook/`](examples/cookbook/). Each guide is self-contained and
solves one concrete problem.

| # | Guide | What you'll get |
|---|---|---|
| 01 | [Write a Charter for an accountant](docs/cookbook/01-write-charter-for-accountant.md) | A signed Charter for an Acme accountant — profile → issue → inspect |
| 02 | [Chain with budget](docs/cookbook/02-chain-with-budget.md) | A two-hop chain that propagates a per-task budget via `operational_limit` |
| 03 | [Revoke without breaking in-flight](docs/cookbook/03-revoke-without-breaking-inflight.md) | A grace-period pattern so revocation does not abort running tasks |
| 04 | [Integrate Stripe](docs/cookbook/04-integrate-stripe.md) | A Charter-gated Stripe payment call (mocked SDK, real flow) |
| 05 | [Integrate OpenAI Agents SDK](docs/cookbook/05-integrate-openai-agents-sdk.md) | `charter_preflight` + `@charter_gated` decorator end-to-end |
| 06 | [Integrate Anthropic SDK](docs/cookbook/06-integrate-anthropic-sdk.md) | Template hook points for the Anthropic SDK (adapter pending) |
| 07 | [Profile YAML best practices](docs/cookbook/07-profile-yaml-best-practices.md) | How to phrase clauses so the LLM grader stays accurate |
| 08 | [Self-host `.well-known/charter`](docs/cookbook/08-self-host-well-known.md) | Publish Charters on your own domain at `/.well-known/charter/<agent_id>` |
| 09 | [Deploy a JWKS and rotate keys](docs/cookbook/09-deploy-jwks.md) | Stand up a JWKS endpoint and rotate the issuer key without breaking callers |
| 10 | [Audit the transparency log](docs/cookbook/10-audit-transparency-log.md) | Walk a full `charter audit verify` / `charter audit show` audit |

See [`docs/cookbook/README.md`](docs/cookbook/README.md) for conventions
and scope notes.

---

## Install

`uv` is the recommended toolchain:

```bash
uv venv
uv pip install -e .
```

Or with plain pip:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

You'll get three console scripts on your `PATH`: `charter`, `charter-mcp`,
`charter-server`.

## Configure

```bash
cp .env.example .env
# Edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-...
```

The `ANTHROPIC_API_KEY` is only needed to **issue** new Charters (the
projection step calls Claude once to expand a profile into clauses).
**Fetching, verifying, and aggregating** verdicts requires no API key —
the MCP server itself makes zero LLM calls at runtime.

Override defaults with environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `CHARTER_URL_BASE` | `http://localhost:8000` | Public base URL where Charters are hosted |
| `CHARTER_DATA_DIR` | `./data` | Where Charters and issuer keys live on disk |
| `CHARTER_MODEL` | `claude-sonnet-4-6` | Anthropic model used by `charter issue` |
| `CHARTER_PORT` | `8000` | Port for `charter-server` |

## Issue a Charter from a profile

Each principal writes a single YAML file describing scope, exclusions,
approval-required actions, data handling, operational limits, and style.
Examples live in [`profiles/`](profiles/).

```bash
charter issue profiles/alice.yaml
charter issue profiles/bob.yaml
```

Each `charter issue` call runs projection (one LLM call), signs with the
issuer's Ed25519 key (auto-generated on first issue), and saves the Charter
to `data/charters/`.

## Host the Charters

```bash
charter-server
# Listens on http://localhost:8000
```

Endpoints:

  - `GET /`                                      — index of all hosted Charters
  - `GET /<principal_id>/<agent_id>`             — Public Charter JSON
  - `GET /api/lookup?principal_id=…&agent_id=…`  — directory resolution

## Inspect from the CLI

```bash
charter inspect <principal_id> <agent_id>
```

## Deployment (fly.io)

The repo ships a `Dockerfile`, a `fly.toml` template, and a GitHub
Actions workflow (`.github/workflows/deploy.yml`) that deploys on every
push to `main`. The pieces:

1.  **One-time provisioning.** The human deployer runs locally:

    ```bash
    fly launch --copy-config --no-deploy   # claims an app name
    fly volumes create charter_data --size 1  # persistent Charter + key store
    ```

    Then edits `fly.toml`'s `app = "..."` line to match the claimed name.

2.  **Secrets.** Configure both fly and GitHub:

    | Where | Name | Purpose |
    |---|---|---|
    | `fly secrets set` | `CHARTER_KEY_PASSPHRASE` | Encrypts issuer Ed25519 private keys at rest (v0.6). |
    | `fly secrets set` | `ANTHROPIC_API_KEY` | Required only by `charter issue`. The server itself doesn't need it. |
    | `fly secrets set` | `CHARTER_URL_BASE` | `https://<your-app>.fly.dev` |
    | GitHub repo secret | `FLY_API_TOKEN` | Lets the workflow run `flyctl deploy`. |
    | GitHub repo variable | `DEPLOY_ENABLED` | Set to `"true"` to enable the deploy workflow. Leave unset to skip deploys until you're ready. |

3.  **`fly.toml` already sets** the production env vars:

    ```toml
    [env]
      CHARTER_PORT     = "8000"
      CHARTER_DATA_DIR = "/data"
      CHARTER_LOG_FORMAT = "json"
    ```

4.  **Deploy.** Push to `main`. The workflow:
    - Builds the multi-stage image.
    - Runs `flyctl deploy --remote-only`.
    - Smoke-checks `https://<app>.fly.dev/healthz` after the rollout
      (retries 5 times to absorb the brief TLS-cert warmup).

5.  **Roll back** with `fly releases list` + `fly deploy --image
    <previous-tag>`. The fly health check on `/healthz` already
    auto-rolls failed deploys before they take traffic; this is the
    "I changed my mind" path.

## Use from a calling agent (MCP)

Any MCP-capable client (Claude Code, Codex CLI, Cursor, custom agents…)
can connect to the `charter` MCP server:

```json
{
  "mcpServers": {
    "charter": {
      "command": "charter-mcp",
      "env": {
        "CHARTER_URL_BASE": "http://localhost:8000",
        "CHARTER_DATA_DIR": "./data"
      }
    }
  }
}
```

This exposes six tools:

| Tool | Purpose | LLM calls inside the tool |
|---|---|---|
| `fetch_charter(url)` | Pull + verify signature, return Charter + protocol hints | 0 |
| `aggregate_verdict(charter, hits)` | Deterministically combine per-clause judgments into a Verdict | 0 |
| `delegate_task(principal, agent, task)` | Calling agent → write task to inbox | 0 |
| `check_inbox()` | Worker agent → read pending task | 0 |
| `send_result(task_id, verdict, …)` | Worker agent → write reply to outbox | 0 |
| `read_outbox()` | Calling agent → read worker's reply | 0 |

The protocol's central design choice: **the LLM judgment lives in the
calling agent, not in the Charter server.** The server only fetches,
verifies, and aggregates — making it stateless, low-cost, and auditable.

See [`AGENTS.md`](AGENTS.md) for the 5-step loop that worker agents follow
under the protocol, and [`docs/CODEX_SETUP.md`](docs/CODEX_SETUP.md) for
a concrete local wiring example with Codex CLI + Claude Code.

---

## Integrations

Framework- and protocol-side adapters live under
[`charter/adapters/`](charter/adapters/). Each adapter is a thin
wrapper around Charter's own MCP/SDK surface — the protocol logic
stays in `charter/`, the adapter only handles the host's wiring.

| Adapter | Module | Purpose |
|---|---|---|
| **OpenAI Agents SDK** | [`charter.adapters.openai_agents`](charter/adapters/openai_agents.py) | `@charter_gated` decorator for `function_tool`-style tool functions, plus a one-call `charter_preflight()` helper. |
| **AP2 Mandate** | [`charter.adapters.ap2`](charter/adapters/ap2.py) | `embed_charter_in_mandate()` writes `extensions.charter_url` into an AP2 mandate; `verify()` runs both the AP2 mandate integrity check and Charter compatibility check and collapses them into one `AP2VerifyResult`. |

### AP2 Mandate adapter

Carry a `charter_url` inside an AP2 mandate envelope so a payment-side
verifier can ask both "is this transaction authorized?" (AP2) and "is
this agent allowed to do this kind of work for this principal at all?"
(Charter) in a single call. The final decision collapses both layers:
either layer saying no forces an `incompatible` (or `needs_approval`)
result — only a clean pass on both yields `allow`.

```python
from charter.adapters.ap2 import embed_charter_in_mandate, verify

# Issuer side: tag the mandate with the agent's Charter.
mandate = embed_charter_in_mandate(
    {
        "payer": "alice@acme.com",
        "payee": "merchant@example.com",
        "amount": {"value": 200.0, "currency": "USD"},
        "task": "Pay $200 for invoice #123.",
        "signature": "<AP2 signature blob>",
    },
    "https://acme.example.com/alice@acme.com/pay_agent_v1",
)

# Verifier side: one call, two layers checked.
result = verify(mandate)
print(result.final_decision)  # "allow" | "needs_approval" | "incompatible"
print(result.reason)
```

`fetch_charter_fn` and `hits_grader` are injectable so hosts can plug
in their own Charter cache layer or keep all LLM traffic on a single
provider. See
[`examples/ap2_charter_demo.py`](examples/ap2_charter_demo.py) for an
end-to-end demo, and the AP2 cookbook chapter (PLANNED B3.9) for the
production deployment walkthrough.

> This adapter does NOT take a dependency on the AP2 SDK — the
> mandate is typed as `dict` and the field shape is documented in the
> module docstring. The mandate-signature check is a mock entry point;
> hosts wire their real AP2 verifier into `_check_mandate` (see the
> adapter source for the contract).

---

## Repo layout

```
agent contract/
├── CONTEXT.md                          protocol glossary
├── AGENTS.md                           worker-agent instructions (Codex / Claude Code)
├── README.md                           this file
├── ROADMAP.md                          prioritized next work
├── LICENSE                             Apache 2.0
├── Dockerfile                          multi-stage image; `charter-server` as CMD
├── fly.toml                            fly.io deployment template
├── .github/workflows/ci.yml            lint + mypy + pytest matrix
├── PRODUCT.md                          canonical product doc (spec + rationale + state + roadmap)
├── docs/
│   ├── CODEX_SETUP.md                  local MCP wiring example
│   └── legacy/
│       └── hackathon-design.md         original hackathon design doc
├── profiles/                           example principal profiles
├── charter/                            Python package
│   ├── constants.py                    protocol constants (TYPE_TO_DECISION, precedence)
│   ├── errors.py                       typed exception hierarchy
│   ├── schema.py                       Pydantic models
│   ├── prompts.py                      LLM system prompt for projection
│   ├── projection.py                   profile.yaml -> Charter (1 LLM call)
│   ├── signing.py                      Ed25519 + Self-Attesting Charter
│   ├── storage.py                      JSON / PEM file I/O
│   ├── server.py                       FastAPI host (incl. /healthz + .well-known)
│   ├── mcp_server.py                   MCP server with 6 tools
│   └── cli.py                          charter issue / charter inspect
├── scripts/
│   └── seed_demo.py                    seed Charters without an LLM call
├── tests/
│   ├── test_smoke.py                   protocol-layer + sign/verify roundtrip
│   ├── test_errors.py                  typed-exception paths
│   └── test_server.py                  FastAPI route coverage
└── data/                               runtime-generated, git-ignored
```

## What this implementation does not do yet

See [`ROADMAP.md`](ROADMAP.md) for the prioritized post-v0 work. Highlights
of what's still ahead:

- **`propose_within_scope` MCP tool** — designed, not yet implemented (v0.6)
- **`charter revoke` / `charter renew` CLI commands** — schema supports them; CLI does not (v0.6)
- **Loopback verification for rewrites** (v0.6)
- **`resolve_charter_url` SDK helper** (v0.6)
- **Encrypted private keys at rest** (v0.6)
- **JWKS endpoint, key-fingerprint pinning, transparency log** — beyond Self-Attesting v0 trust (v0.8+)
- **Charter Chain attenuation** — multi-hop delegation with `B ⊆ A` enforcement (v0.7)
- **Framework SDK adapters** (LangGraph, OpenAI Agents, CrewAI) — first one in v0.7
- **Web Bot Auth / AP2 integration** (v0.8+)

What's done in v0.5 (this iteration): Apache 2.0 license, typed exceptions
(`charter.errors`), `.well-known/charter/{agent_id}` self-hosted mode,
`/healthz` endpoint, Dockerfile + fly.toml deployment template, ruff +
mypy --strict in CI on `{py3.12, py3.13} × {ubuntu, macos, windows}`, and
the doc split (later merged into [`PRODUCT.md`](PRODUCT.md) post-v0.7).

---

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
