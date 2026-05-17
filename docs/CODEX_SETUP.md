# Codex CLI Setup for Charter Demo

One-time setup so Codex picks up the `charter` MCP server and the
`AGENTS.md` instructions in this project.

## 1. Make sure `charter-mcp` is installed

You've already done this if `pytest tests/test_smoke.py` passed earlier.
If not:

```bash
cd "C:/Users/Henry Ma/Desktop/agent contract"
uv venv
uv pip install -e .
```

That creates `.venv/Scripts/charter-mcp.exe`. Sanity check:

```bash
ls .venv/Scripts/charter-mcp.exe
```

Should exist and be ~46 KB.

## 2. Configure Codex CLI to load the MCP server

Edit `~/.codex/config.toml` (create if it doesn't exist). On Windows that
expands to `C:\Users\Henry Ma\.codex\config.toml`. Add:

```toml
[mcp_servers.charter]
command = "C:/Users/Henry Ma/Desktop/agent contract/.venv/Scripts/charter-mcp.exe"

[mcp_servers.charter.env]
CHARTER_URL_BASE = "http://localhost:8000"
CHARTER_DATA_DIR = "C:/Users/Henry Ma/Desktop/agent contract/data"
```

Notes:

- Use **forward slashes** in the path even on Windows — TOML is happier
  that way, and Codex normalizes it.
- The absolute path to `charter-mcp.exe` avoids any PATH-resolution
  surprises.
- `CHARTER_DATA_DIR` MUST match the directory `charter-server` is reading
  from, otherwise the inbox/outbox files won't sync.

## 3. Start the supporting services (one terminal each)

Terminal 1 — Charter host (FastAPI on port 8000):

```bash
cd "C:/Users/Henry Ma/Desktop/agent contract"
.venv/Scripts/charter-server.exe
```

Leave this running. Browser-check: open
http://localhost:8000/ — you should see two active Charters.

Terminal 2 — Claude Code (calling agent), launched from the project dir:

```bash
cd "C:/Users/Henry Ma/Desktop/agent contract"
claude    # or however you launch Claude Code
```

Claude Code also needs the `charter` MCP. The **project-scoped** config lives
in `.mcp.json` at the **project root** (not inside `.claude/` — that
location is silently ignored). The file should already exist in this repo:

```json
{
  "mcpServers": {
    "charter": {
      "command": "C:/Users/Henry Ma/Desktop/agent contract/.venv/Scripts/charter-mcp.exe",
      "args": [],
      "env": {
        "CHARTER_URL_BASE": "http://localhost:8000",
        "CHARTER_DATA_DIR": "C:/Users/Henry Ma/Desktop/agent contract/data"
      }
    }
  }
}
```

On first launch in this directory, Claude Code will prompt for approval
before connecting to the project-scoped server (security default). Approve
once and the MCP loads on every subsequent session.

Confirm by running `/mcp` inside Claude Code — `charter` should appear
with 6 tools.

Terminal 3 — Codex (target agent), launched from the project dir so
`AGENTS.md` is auto-loaded:

```bash
cd "C:/Users/Henry Ma/Desktop/agent contract"
codex
```

In the first Codex prompt, sanity-check the wiring:

```
> What tools do you have under the charter namespace?
```

You should see at least: `fetch_charter`, `aggregate_verdict`, `check_inbox`,
`send_result`, `delegate_task`, `read_outbox`.

## 4. Run the demo

In Claude Code (Terminal 2):

```
> Delegate a task to research_agent_v1 acting for alice@acme.com:
> "write a React pricing component"
```

Claude Code calls `delegate_task(...)`, writes inbox.json, and starts
watching outbox.json.

In Codex (Terminal 3):

```
> check inbox
```

Codex runs the 5-step loop from AGENTS.md, fetches Alice's Charter,
judges clauses, aggregates, and replies via `send_result(...)` with
verdict=incompatible and applied clause C-002.

Back in Claude Code, the file watcher fires and Claude Code reports the
reply automatically.

Then in Codex:

```
> switch your principal to bob@startup.io
```

In Claude Code:

```
> Re-delegate the same task to research_agent_v1, this time for bob@startup.io.
```

Codex runs the loop again — this time fetches Bob's Charter, gets
verdict=allow, and actually writes the React component before replying.

## 5. PocketOS save scene (closing beat)

In Claude Code:

```
> Delegate to bob@startup.io: "DROP TABLE acme_invoices_2023"
```

Codex fetches Bob's Charter, C-004 hits at confidence 0.96,
aggregate_verdict returns `needs_approval`. Codex declines, awaits
human approval. The PocketOS 9-second drop is averted.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `charter-mcp` not found | Use the absolute path in `~/.codex/config.toml` and `~/.claude/mcp_servers.json` |
| `CharterNotFoundError: GET ... failed` | `charter-server` not running, or wrong port. `curl http://localhost:8000/` should return JSON. |
| `CharterSignatureError` | Charter JSON corrupted on disk. Re-run `python scripts/seed_demo.py profiles/<x>.yaml`. |
| Inbox not visible to the other side | `CHARTER_DATA_DIR` mismatched between the two MCP server instances. Both must point to the same `data/` directory. |
| `mcp_server.charter`: connection closed | The MCP server crashed — usually a venv path issue. Run `charter-mcp` directly to see the error. |
