"""profile.yaml -> Charter draft via one Anthropic LLM call.

The LLM only fills in `clauses[]` and `summary.plain_language`. All metadata
(binding, principal, issuer, agent_operator, lifecycle, provenance) is filled
deterministically from the profile and the issuer keypair.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .constants import DEFAULT_MODEL
from .prompts import PROJECTION_SYSTEM
from .schema import (
    AgentOperator,
    Binding,
    Charter,
    Clause,
    Issuer,
    Lifecycle,
    Principal,
    Profile,
    Provenance,
    SourceCommitment,
    Summary,
)
from .signing import public_key_to_string


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

def load_profile(path: Path) -> tuple[Profile, str]:
    """Load and validate a profile.yaml. Returns (Profile, raw_text)."""
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return Profile.model_validate(data), raw


def _hash_profile(raw: str) -> str:
    """SHA-256 commitment of the raw profile.yaml text."""
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# LLM projection
# ---------------------------------------------------------------------------

def _project_clauses_via_llm(profile: Profile) -> tuple[list[Clause], str]:
    """Call Claude to expand the profile into clauses + summary."""
    client = anthropic.Anthropic()
    model = os.environ.get("CHARTER_MODEL", DEFAULT_MODEL)

    profile_json = profile.model_dump_json(indent=2)

    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=PROJECTION_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"profile.yaml:\n```json\n{profile_json}\n```",
            }
        ],
    )

    text = "".join(b.text for b in message.content if b.type == "text").strip()
    # Strip accidental markdown fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json"):].lstrip()

    data: dict[str, Any] = json.loads(text)
    clauses = [Clause.model_validate(c) for c in data["clauses"]]
    summary = data["summary_plain_language"]
    return clauses, summary


# ---------------------------------------------------------------------------
# Build full Charter
# ---------------------------------------------------------------------------

def project(
    profile: Profile,
    profile_raw: str,
    issuer_private_key: Ed25519PrivateKey,
) -> Charter:
    """Project a Profile into a fully populated (but unsigned) Charter."""
    clauses, summary_text = _project_clauses_via_llm(profile)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    issued_at = now
    valid_until = now + timedelta(days=profile.lifecycle.valid_days)

    public_key_str = public_key_to_string(issuer_private_key.public_key())

    charter_id = (
        f"charter:{profile.principal.id}:{profile.agent.id}:"
        f"{issued_at.date().isoformat()}"
    )

    return Charter(
        charter_id=charter_id,
        binding=Binding(
            principal_id=profile.principal.id,
            agent_id=profile.agent.id,
        ),
        principal=Principal(
            id=profile.principal.id,
            role_summary=profile.principal.role,
        ),
        issuer=Issuer(
            id=profile.principal.id,
            relationship_to_principal="self",
        ),
        agent_operator=AgentOperator(
            id="generic_worker_agent_provider",
            agent_card_url=profile.agent.card_url,
        ),
        summary=Summary(plain_language=summary_text),
        clauses=clauses,
        lifecycle=Lifecycle(
            issued_at=issued_at,
            valid_until=valid_until,
            status="active",
        ),
        provenance=Provenance(
            issuer_public_key=public_key_str,
            issuer_signature="",  # set later by sign_charter
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description=(
                        f"{profile.principal.id} profile answered on "
                        f"{issued_at.date().isoformat()}"
                    ),
                    content_hash=_hash_profile(profile_raw),
                )
            ],
            generated_at=issued_at,
        ),
    )
