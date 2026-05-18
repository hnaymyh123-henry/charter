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

- **[`docs/spec.md`](docs/spec.md)** — protocol contract: schema, decision rules, lifecycle, MCP tool signatures.
- **[`docs/design.md`](docs/design.md)** — design rationale and tradeoffs (ADR-style).
- **[`CONTEXT.md`](CONTEXT.md)** — protocol glossary.
- **[`ROADMAP.md`](ROADMAP.md)** — prioritized work for upcoming iterations.
- **[`docs/legacy/hackathon-design.md`](docs/legacy/hackathon-design.md)** — original hackathon design doc, kept for historical reference.

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
├── docs/
│   ├── spec.md                         protocol contract
│   ├── design.md                       design rationale
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
the doc split (`docs/spec.md` + `docs/design.md`).

---

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
