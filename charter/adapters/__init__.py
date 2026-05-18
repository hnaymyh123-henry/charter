"""Framework adapters.

Each submodule wraps the Charter MCP/SDK surface for one agent framework
so a single decorator or one-call helper can gate every delegation on
the protocol verdict. The adapters are intentionally thin — the
protocol logic stays in `charter/`, the adapter only handles
framework-specific wiring.

v0.7 ships the OpenAI Agents SDK adapter. LangGraph and CrewAI are on
the v0.8+ roadmap.
"""
