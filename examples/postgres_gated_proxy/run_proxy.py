"""End-to-end runnable demo for the Postgres capability-boundary proxy.

This script:

  1. Generates a fresh Ed25519 keypair and signs a small demo Charter
     in-memory.
  2. Spins up a tiny HTTP server on ``127.0.0.1:8765`` that serves the
     signed Charter at ``/charter``.
  3. Starts :class:`charter.adapters.postgres.CharterGatedProxy` on
     ``127.0.0.1:55432`` forwarding to ``127.0.0.1:5432`` (expected
     to point at a docker'd Postgres — see README).

Run it, then connect with::

    PGOPTIONS="-c charter_url=http://127.0.0.1:8765/charter" \\
        psql "postgresql://postgres:demo@127.0.0.1:55432/postgres" \\
        -c "SELECT * FROM public_reports;"

Hit Ctrl-C to stop. Both the HTTP server and the proxy share the
same asyncio loop, so a single Ctrl-C tears everything down.

Charter scope summary
---------------------

  - ``scope``: read rows from ``public_reports`` for reporting.
  - ``out_of_scope``: anything that mutates ``production_secrets``.

Combined with the proxy's default conservative grader (every clause
hit at full confidence), this means:

  - ``SELECT * FROM public_reports`` -> ``allow``
  - any ``DROP``, ``DELETE``, ``UPDATE``, ``INSERT`` -> ``incompatible``
  - anything that does not parse as recognised SQL -> ``incompatible``
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from aiohttp import web

from charter.adapters.postgres import CharterGatedProxy
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
from charter.signing import generate_keypair, public_key_to_string, sign_charter


def _build_demo_charter() -> Charter:
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    charter = Charter(
        charter_id="charter:demo@local:db_agent_v1:demo",
        binding=Binding(principal_id="demo@local", agent_id="db_agent_v1"),
        principal=Principal(id="demo@local", role_summary="Demo principal."),
        issuer=Issuer(id="demo@local"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(
            plain_language=(
                "Demo charter for the Postgres capability-boundary proxy: "
                "scopes reads from public_reports; refuses mutations."
            )
        ),
        clauses=[
            Clause(
                id="C-001",
                type="scope",
                text="Read rows from public_reports for analytical reporting.",
            ),
            Clause(
                id="C-002",
                type="out_of_scope",
                text="Insert, update, delete, or drop production data.",
            ),
        ],
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
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
    return charter


async def _make_charter_app(charter: Charter) -> web.Application:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(json.loads(charter.model_dump_json()))

    app = web.Application()
    app.router.add_get("/charter", handler)
    return app


async def _main() -> None:
    charter = _build_demo_charter()
    app = await _make_charter_app(charter)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8765)
    await site.start()
    print("Demo Charter served at http://127.0.0.1:8765/charter")

    proxy = CharterGatedProxy(
        bind_host="127.0.0.1",
        bind_port=55432,
        upstream_host="127.0.0.1",
        upstream_port=5432,
    )
    server = await proxy.start_server()
    print("Charter PG proxy listening on 127.0.0.1:55432, upstream 127.0.0.1:5432")
    print("Ctrl-C to stop.")

    try:
        await server.serve_forever()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
