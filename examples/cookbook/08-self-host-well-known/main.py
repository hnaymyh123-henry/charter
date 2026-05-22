"""Cookbook #08 — Self-host /.well-known/charter/<agent_id>.

A principal who wants to publish their own Charters on their own
domain enables "self-hosted mode" by setting one env var:

    CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com

In that mode, `charter-server` exposes
`GET /.well-known/charter/<agent_id>` (in addition to the canonical
`/<principal_id>/<agent_id>` route). The endpoint resolves only this
one principal's Charters; everything else returns 404.

This script:

    1. Seeds Alice's Charter into a scratch data dir.
    2. Starts charter.server.app in-process with
       CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com.
    3. Fetches /.well-known/charter/research_agent_v1 and prints the
       resulting JSON. Compares with the canonical
       /<principal_id>/<agent_id> response to prove they're identical.
    4. Fetches /.well-known/charter/some_other_agent and prints the 404.

Run from the repo root:

    python examples/cookbook/08-self-host-well-known/main.py
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _seed_charter(principal_id: str, agent_id: str) -> None:
    from charter.schema import (
        AgentOperator,
        Binding,
        Charter,
        Clause,
        Issuer,
        Lifecycle,
        Principal,
        Provenance,
        Summary,
    )
    from charter.signing import public_key_to_string, sign_charter
    from charter.storage import ensure_issuer_key, save_charter

    now = datetime.now(UTC).replace(microsecond=0)
    pk = ensure_issuer_key(principal_id)
    charter = Charter(
        charter_id=f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}",
        binding=Binding(principal_id=principal_id, agent_id=agent_id),
        principal=Principal(id=principal_id, role_summary="Senior Accountant"),
        issuer=Issuer(id=principal_id, relationship_to_principal="self"),
        agent_operator=AgentOperator(id="generic_worker_agent_provider"),
        summary=Summary(plain_language="Accounting work allowed."),
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting and tax work"),
        ],
        lifecycle=Lifecycle(
            issued_at=now, valid_until=now + timedelta(days=30), status="active"
        ),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(pk.public_key()),
            issuer_signature="",
            generated_at=now,
        ),
    )
    sign_charter(charter, pk)
    save_charter(charter)


def _start_server() -> tuple[str, object]:
    import uvicorn

    from charter.server import app

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("charter-server did not start within 2.5s")
    return f"http://127.0.0.1:{port}", server


def main() -> int:
    scratch = _ROOT / "data" / "cookbook_08"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)
    os.environ["CHARTER_SELF_HOSTED_PRINCIPAL"] = "alice@acme.com"

    print("=" * 72)
    print("Cookbook #08 — Self-host .well-known/charter/<agent_id>")
    print("=" * 72)
    print()

    _seed_charter("alice@acme.com", "research_agent_v1")
    print("Seeded Charter: alice@acme.com × research_agent_v1")

    base, server = _start_server()
    print(f"Started charter-server with CHARTER_SELF_HOSTED_PRINCIPAL=alice@acme.com")
    print(f"Origin: {base}")
    print()

    try:
        import httpx

        # ---- Well-known happy path -------------------------------------
        url_well_known = f"{base}/.well-known/charter/research_agent_v1"
        resp = httpx.get(url_well_known, timeout=10.0)
        print(f"GET {url_well_known}")
        print(f"  -> HTTP {resp.status_code}")
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
        wk_json = resp.json()
        print(f"  -> charter_id: {wk_json['charter_id']}")
        print(f"  -> binding:    {wk_json['binding']['principal_id']} x {wk_json['binding']['agent_id']}")
        print()

        # ---- Canonical multi-tenant route ------------------------------
        url_canonical = f"{base}/alice@acme.com/research_agent_v1"
        resp_canonical = httpx.get(url_canonical, timeout=10.0)
        print(f"GET {url_canonical}")
        print(f"  -> HTTP {resp_canonical.status_code}")
        assert resp_canonical.status_code == 200
        canonical_json = resp_canonical.json()
        print(f"  -> charter_id: {canonical_json['charter_id']}")
        # Same Charter -> identical body bytes after json round-trip.
        assert wk_json == canonical_json, "Well-known and canonical responses diverge."
        print("  -> identical to /.well-known/charter/research_agent_v1 body")
        print()

        # ---- 404 for an agent the principal doesn't own ----------------
        url_missing = f"{base}/.well-known/charter/nonexistent_agent"
        resp_missing = httpx.get(url_missing, timeout=10.0)
        print(f"GET {url_missing}")
        print(f"  -> HTTP {resp_missing.status_code}  (expected 404)")
        assert resp_missing.status_code == 404, f"expected 404, got {resp_missing.status_code}"
        print()

        # ---- JWKS in self-hosted mode is filtered to this principal ----
        url_jwks = f"{base}/.well-known/jwks.json"
        resp_jwks = httpx.get(url_jwks, timeout=10.0)
        print(f"GET {url_jwks}")
        jwks_json = resp_jwks.json()
        principals_in_jwks = {k.get("iss") for k in jwks_json["keys"]}
        print(f"  -> keys: {len(jwks_json['keys'])}  issuers: {principals_in_jwks}")
        assert principals_in_jwks == {"alice@acme.com"}, (
            f"In self-hosted mode JWKS should only list alice@acme.com; got {principals_in_jwks}"
        )
        print()

        print("[OK] /.well-known/charter/<agent_id> serves only this principal's Charters,")
        print("     JWKS endpoint filters to the same principal, and unknown agents 404.")
        return 0
    finally:
        server.should_exit = True  # type: ignore[attr-defined]


if __name__ == "__main__":
    sys.exit(main())
