# Charter

> Agent 经济的雇佣合同 — Authority 层
> v0 hackathon demo, 2026-05-17

Charter is the missing layer between **Capability** (Agent Card) and
**Authorization** (AP2 Mandate): it answers _"this agent acts for whom, under
what continuing constraints?"_

See `Charter-黑客松项目文档.md` for the full design and `CONTEXT.md` for the
glossary.

---

## Quick start (local, 3-minute demo)

### 1. Install

```bash
# uv recommended (装包秒级):
uv venv
uv pip install -e .

# Or plain pip:
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and paste your Anthropic API key:
#   ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Issue two Charters (one underlying agent, two principals)

```bash
charter issue profiles/alice.yaml
charter issue profiles/bob.yaml
```

You should see two `✓ Charter active` lines with different `charter_url`s.

### 4. Start the host

```bash
charter-server
# Listens on http://localhost:8000
```

Open in a browser:

  - http://localhost:8000/                                  — index of all Charters
  - http://localhost:8000/alice@acme.com/research_agent_v1  — Alice's Charter JSON
  - http://localhost:8000/bob@startup.io/research_agent_v1  — Bob's Charter JSON

### 5. Inspect from the CLI

```bash
charter inspect alice@acme.com research_agent_v1
charter inspect bob@startup.io research_agent_v1
```

### 6. Plug into Claude Code

Add to `~/.claude/mcp_servers.json` (or your project's MCP config):

```json
{
  "mcpServers": {
    "charter": {
      "command": "charter-mcp"
    }
  }
}
```

Then in Claude Code, ask:

  - _"Use the charter MCP to fetch http://localhost:8000/alice@acme.com/research_agent_v1, then check whether I should write a React component."_
  - _"Now check the same task against http://localhost:8000/bob@startup.io/research_agent_v1."_
  - _"Try `DROP TABLE acme_invoices_2023` against Bob's Charter."_

Each call hits one of the three MCP tools:

| Tool | Purpose | LLM calls |
|---|---|---|
| `fetch_charter(url)` | Pull + verify signature | 0 |
| `check_compatibility(url, task)` | Per-clause hit grading + protocol aggregation | 1 |
| `propose_within_scope(url, task, verdict)` | Single-shot in-scope rewrite (no loopback in v0) | 1 |

---

## Repo layout

```
agent contract/
├── CONTEXT.md
├── Charter-黑客松项目文档.md
├── profiles/                       # demo principals
│   ├── alice.yaml
│   └── bob.yaml
├── charter/
│   ├── constants.py                # TYPE_TO_DECISION protocol map
│   ├── schema.py                   # Pydantic models
│   ├── prompts.py                  # LLM system prompts
│   ├── projection.py               # profile.yaml -> Charter (1 LLM call)
│   ├── signing.py                  # Ed25519 + Self-Attesting
│   ├── storage.py                  # JSON / PEM file I/O
│   ├── server.py                   # FastAPI host
│   ├── mcp_server.py               # fastmcp 3 tools
│   └── cli.py                      # `charter issue` / `charter inspect`
├── data/                           # runtime-generated, git-ignored
│   ├── charters/*.json
│   └── keys/*.pem
└── tests/test_smoke.py
```

---

## What's deliberately out of scope for v0

See _§ 16. v0 实施范围速查表_ in `Charter-黑客松项目文档.md` for the full
40-decision breakdown. Highlights:

- **No loopback verification or retry inside `propose_within_scope`.** v0 returns the first LLM rewrite as-is.
- **No `.well-known` self-hosted mode.** Profile points at the local SaaS host.
- **No `charter revoke` / `charter renew` CLI commands.** Schema supports them, CLI does not.
- **No `service_attestation` second-layer signature.** HTTPS + self-attesting key is the entire trust model.
- **No JWKS endpoint, no TOFU pinning, no transparency log.** All listed as v0+ in §14.
- **No Charter Chain attenuation.** Demo Act 2 is single-Charter check only.

---

## License

Hackathon prototype. No license declared yet.
