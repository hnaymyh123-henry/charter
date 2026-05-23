"""Cookbook #09 — Deploy a JWKS and rotate keys.

Walks the full key rotation lifecycle:

    1. Issue a Charter (key K1, fingerprint F1). Server publishes K1 at
       /.well-known/jwks.json. Calling agent fetches the Charter -> pins F1.
    2. Rotate: generate a fresh key K2, replace the issuer's PEM on disk,
       reissue the Charter so it carries K2. JWKS now publishes K2.
    3. Calling agent re-fetches without resetting its pin: gets
       CharterPinMismatchError (this is the protection).
    4. Operator runs `charter pins reset <principal>` (the cookbook
       calls `reset_pin(...)` directly). Re-fetch succeeds; the new
       pin matches F2.

Run from the repo root:

    python examples/cookbook/09-deploy-jwks/main.py
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


def _build_and_save_charter(principal_id: str, agent_id: str) -> str:
    """Build + sign a Charter with whatever key is currently on disk."""
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
        clauses=[Clause(id="C-001", type="scope", text="Accounting work")],
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
    return public_key_to_string(pk.public_key())


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


def _rotate_key(principal_id: str) -> None:
    """Replace the issuer's PEM on disk with a freshly generated keypair.

    This is the manual rotation flow. A production setup would run a
    similar code path inside an operator script: generate -> save ->
    reissue every active Charter for this principal.
    """
    from charter.keys import clear_cache
    from charter.signing import generate_keypair, save_private_key
    from charter.storage import key_path

    new_private, _ = generate_keypair()
    save_private_key(new_private, key_path(principal_id))
    # In real deployments callers see the new JWKS after their TTL expires
    # (5 min default). We flush in-process so the cookbook is deterministic.
    clear_cache()


def main() -> int:
    scratch = _ROOT / "data" / "cookbook_09"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["CHARTER_DATA_DIR"] = str(scratch)
    os.environ["CHARTER_SELF_HOSTED_PRINCIPAL"] = "alice@acme.com"
    # The pin file lives under CHARTER_DATA_DIR by default, so the
    # cookbook's pin operations stay isolated to data/cookbook_09/.

    print("=" * 72)
    print("Cookbook #09 — Deploy a JWKS and rotate keys")
    print("=" * 72)
    print()

    principal_id, agent_id = "alice@acme.com", "research_agent_v1"

    import httpx

    from charter.errors import CharterPinMismatchError
    from charter.mcp_server import _fetch_and_verify
    from charter.pins import fingerprint_of, get_pin, reset_pin

    # ---- Step 1: issue under K1; fetch -> establishes pin F1 ------------
    public_k1 = _build_and_save_charter(principal_id, agent_id)
    print(f"step 1  Issued Charter under K1.")
    print(f"        K1 public:  {public_k1[:40]}...")
    print(f"        K1 fingerprint: {fingerprint_of(public_k1)}")

    base, server = _start_server()
    print(f"        charter-server up at {base}")
    print()
    try:
        # JWKS publishes K1.
        jwks_k1 = httpx.get(f"{base}/.well-known/jwks.json", timeout=10).json()
        print(f"step 1  GET /.well-known/jwks.json -> {len(jwks_k1['keys'])} key(s)")
        for jwk in jwks_k1["keys"]:
            print(f"          kid={jwk['kid']}  iss={jwk.get('iss')}")
        print()

        charter_url = f"{base}/{principal_id}/{agent_id}"
        charter1 = _fetch_and_verify(charter_url)
        pin1 = get_pin(principal_id)
        assert pin1 is not None
        print(f"step 1  fetch_and_verify ok; pin recorded: {pin1.fingerprint}")
        print()

        # ---- Step 2: rotate to K2 + reissue Charter ---------------------
        _rotate_key(principal_id)
        public_k2 = _build_and_save_charter(principal_id, agent_id)
        assert public_k1 != public_k2, "rotation should yield a different key"
        print(f"step 2  Rotated to K2; reissued Charter.")
        print(f"        K2 public:  {public_k2[:40]}...")
        print(f"        K2 fingerprint: {fingerprint_of(public_k2)}")
        print()

        # JWKS now publishes K2 (the live key on disk).
        jwks_k2 = httpx.get(f"{base}/.well-known/jwks.json", timeout=10).json()
        kids_k2 = {jwk["kid"] for jwk in jwks_k2["keys"]}
        kids_k1 = {jwk["kid"] for jwk in jwks_k1["keys"]}
        assert kids_k1 != kids_k2, "JWKS kid set should change after rotation"
        print(f"step 2  GET /.well-known/jwks.json -> kid set changed {sorted(kids_k1)} -> {sorted(kids_k2)}")
        print()

        # ---- Step 3: caller without reset -> CharterPinMismatchError ----
        try:
            _fetch_and_verify(charter_url)
            print("step 3  unexpected: fetch succeeded without pin reset")
            return 1
        except CharterPinMismatchError as e:
            print(f"step 3  expected refusal: CharterPinMismatchError raised.")
            print(f"        message: {str(e)[:90]}...")
        print()

        # ---- Step 4: operator runs `charter pins reset` -----------------
        reset_pin(principal_id)
        print(f"step 4  reset_pin({principal_id!r}) -> dropped old pin")

        # Re-fetch establishes a fresh pin against F2.
        charter2 = _fetch_and_verify(charter_url)
        pin2 = get_pin(principal_id)
        assert pin2 is not None
        assert pin2.fingerprint == fingerprint_of(public_k2)
        print(f"        fetch_and_verify ok; new pin: {pin2.fingerprint}")
        print(f"        old pin == new pin? {pin1.fingerprint == pin2.fingerprint}")
        print()

        print("[OK] Rotation flow: pin catches surprise rotation; reset re-establishes trust.")
        return 0
    finally:
        server.should_exit = True  # type: ignore[attr-defined]


if __name__ == "__main__":
    sys.exit(main())
