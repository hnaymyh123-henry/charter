"""CFO Office charter seeding (profile.yaml -> signed Charter, deterministically).

`seed_cfo_office(base_url)` is the entry point the orchestrator's `run_demo`
prefers over its tiny inline fallback. It turns the four `profiles/*.yaml`
Principal-Context files into four signed worker Charters, so the demo's
charters are genuinely *driven by* the profiles rather than hand-built in code.
"""

from .seed_cfo_office import seed_cfo_office

__all__ = ["seed_cfo_office"]
