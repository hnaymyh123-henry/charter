"""Demo: Web Bot Auth gated FastAPI middleware (Issue #28 / A6).

This example runs entirely in-process — no external Charter server, no
real network. It shows the four code paths the middleware must handle:

  (a) Legal request — valid signature + allow verdict → 200.
  (b) Missing Signature header               → 403.
  (c) Signature signed with a wrong key      → 403 (invalid signature).
  (d) Charter verdict = incompatible         → 403 (decision = incompatible).

Run:

    python examples/web_bot_auth_middleware.py

Expected output (one line per scenario)::

    (a) GET /search OK       -> 200 {"hits": [...]}
    (b) missing signature    -> 403 web_bot_auth_signature_invalid
    (c) wrong key signature  -> 403 web_bot_auth_signature_invalid
    (d) out-of-scope task    -> 403 charter_incompatible (clauses: C-002)
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:  # noqa: PLR0915 — single demo function, 4 inline scenarios
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from charter.adapters import web_bot_auth
    from charter.adapters.web_bot_auth import sign_request
    from charter.schema import (
        AgentOperator,
        Binding,
        Charter,
        Clause,
        Issuer,
        Lifecycle,
        Principal,
        Provenance,
        SourceCommitment,
        Summary,
    )
    from charter.signing import (
        generate_keypair,
        kid_for_public_key,
        public_key_to_jwk,
        public_key_to_string,
        sign_charter,
    )

    # 1) Issuer keypair + signed Charter. In the real world the issuer
    #    would publish this via `charter-server` and the JWKS via
    #    `/.well-known/jwks.json`; we mock both for the demo.
    private, public = generate_keypair()
    pk_str = public_key_to_string(public)
    key_id = kid_for_public_key(pk_str)
    jwks = {key_id: public_key_to_jwk(pk_str, kid=key_id)}

    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:alice@acme.com:research_agent_v1:2026-05-22",
        binding=Binding(principal_id="alice@acme.com", agent_id="research_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Research analyst."),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="demo-operator"),
        summary=Summary(plain_language="Public web research only; no PII export."),
        clauses=[
            Clause(id="C-001", type="scope", text="Read public web content."),
            Clause(id="C-002", type="out_of_scope", text="Customer PII export."),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=pk_str,
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="demo",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(charter, private)

    charter_url = "https://charter.example.com/alice@acme.com/research_agent_v1"

    # 2) Trivial grader: tag /search as scope C-001 (allow), /export as
    #    out_of_scope C-002 (incompatible). A real deployment would
    #    plug in their LLM grader.
    def grader(_c: Charter, task: str) -> list[dict[str, Any]]:
        if "/export" in task:
            return [{"id": "C-002", "hit": True, "confidence": 0.95, "reason": "PII export"}]
        return [{"id": "C-001", "hit": True, "confidence": 0.9, "reason": "public read"}]

    # 3) Build the target app + install the gated middleware. We hand the
    #    middleware in-process fakes for fetch_charter_fn and
    #    fetch_jwks_fn so this example needs no network.
    app = FastAPI()

    @app.get("/search")
    def search() -> dict[str, Any]:
        return {"hits": ["doc-1", "doc-2"]}

    @app.get("/export")
    def export() -> dict[str, Any]:
        return {"records": [{"name": "bob"}, {"name": "carol"}]}

    app.add_middleware(
        web_bot_auth._WebBotAuthMiddleware,
        fetch_charter_fn=lambda _url: charter,
        fetch_jwks_fn=lambda _origin: jwks,
        hits_grader=grader,
        task_from=None,
        cache_size=8,
    )

    client = TestClient(app)

    # ------------------------------------------------------------------
    # (a) Legal request: sign + send -> 200
    # ------------------------------------------------------------------
    url = "http://testserver/search"
    signed = sign_request(
        "GET",
        url,
        {},
        b"",
        charter_url=charter_url,
        private_key=private,
        key_id=key_id,
    )
    resp = client.get("/search", headers=signed)
    print(f"(a) GET /search OK       -> {resp.status_code} {json.dumps(resp.json())}")

    # ------------------------------------------------------------------
    # (b) No Signature header at all -> 403 web_bot_auth_signature_invalid
    # ------------------------------------------------------------------
    resp = client.get("/search")
    print(f"(b) missing signature    -> {resp.status_code} {resp.json()['error_code']}")

    # ------------------------------------------------------------------
    # (c) Sign with a foreign key that the JWKS does not list -> 403
    # ------------------------------------------------------------------
    wrong_private, wrong_public = generate_keypair()
    wrong_pk_str = public_key_to_string(wrong_public)
    wrong_kid = kid_for_public_key(wrong_pk_str)
    bad_signed = sign_request(
        "GET",
        url,
        {},
        b"",
        charter_url=charter_url,
        private_key=wrong_private,
        key_id=wrong_kid,
    )
    resp = client.get("/search", headers=bad_signed)
    print(f"(c) wrong key signature  -> {resp.status_code} {resp.json()['error_code']}")

    # ------------------------------------------------------------------
    # (d) Valid signature, but grader marks the task as out_of_scope -> 403
    # ------------------------------------------------------------------
    url2 = "http://testserver/export"
    signed2 = sign_request(
        "GET",
        url2,
        {},
        b"",
        charter_url=charter_url,
        private_key=private,
        key_id=key_id,
    )
    resp = client.get("/export", headers=signed2)
    payload = resp.json()
    print(
        f"(d) out-of-scope task    -> {resp.status_code} {payload['error_code']} "
        f"(clauses: {','.join(payload['applied_clauses'])})"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
