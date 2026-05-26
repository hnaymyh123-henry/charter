"""Tests for ``charter.adapters.postgres.proxy``.

Two test layers:

  - **Wire-protocol unit tests** that drive the proxy with raw bytes
    and verify it gates correctly. No real Postgres is needed; a fake
    upstream asyncio server consumes the forwarded traffic and echoes
    a minimal ``ReadyForQuery`` so the proxy completes setup. These
    tests are the AC for "the proxy refuses incompatible SQL with a
    PG ErrorResponse" without paying for a docker pull.
  - **Real-Postgres integration tests** marked ``requires_postgres``
    that skip cleanly when the env does not expose ``docker`` or
    ``testcontainers`` is unavailable. They are the AC for "the proxy
    end-to-end forwards an allow-verdict SELECT to a real PG".
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import struct
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# Proxy + gate transitively import `sqlglot` (in the `postgres_proxy` optional
# extra). Skip the whole module if it isn't installed so a `[dev]`-only env
# collects cleanly. Real-Postgres tests below have their own `requires_postgres`
# marker on top of this.
pytest.importorskip("sqlglot")

from charter.adapters.postgres.gate import _default_hits_grader  # noqa: E402
from charter.adapters.postgres.proxy import CharterGatedProxy, _build_error_response  # noqa: E402
from charter.errors import CharterNotFoundError
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

# ---------------------------------------------------------------------------
# Charter fixtures
# ---------------------------------------------------------------------------


def _build_charter(
    *,
    scope_text: str = "Read public reporting data.",
    out_of_scope_text: str | None = "Modify or drop production tables.",
) -> Charter:
    """Build a small, signed Charter for proxy tests.

    The default Charter has one ``scope`` clause and one ``out_of_scope``
    clause. Combined with the conservative default grader ("all clauses
    hit"), the aggregator returns ``incompatible`` — which is exactly
    what we want for the refusal-path tests. Tests that need an
    allow-path can pass ``out_of_scope_text=None`` to drop the
    restrictive clause.
    """
    private, public = generate_keypair()
    now = datetime.now(UTC).replace(microsecond=0)
    clauses = [Clause(id="C-001", type="scope", text=scope_text)]
    if out_of_scope_text:
        clauses.append(Clause(id="C-002", type="out_of_scope", text=out_of_scope_text))
    c = Charter(
        charter_id="charter:alice@acme.com:db_agent_v1:test",
        binding=Binding(principal_id="alice@acme.com", agent_id="db_agent_v1"),
        principal=Principal(id="alice@acme.com", role_summary="Test principal."),
        issuer=Issuer(id="alice@acme.com"),
        agent_operator=AgentOperator(id="generic"),
        summary=Summary(plain_language="Charter for the PG proxy tests."),
        clauses=clauses,
        lifecycle=Lifecycle(issued_at=now, valid_until=now + timedelta(days=30)),
        provenance=Provenance(
            issuer_public_key=public_key_to_string(public),
            issuer_signature="",
            source_commitments=[
                SourceCommitment(
                    type="profile_yaml",
                    description="t",
                    content_hash="sha256:" + "0" * 64,
                )
            ],
            generated_at=now,
        ),
    )
    sign_charter(c, private)
    return c


# ---------------------------------------------------------------------------
# Fake upstream PG (just enough to let the proxy complete its setup)
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """A trivial asyncio server that mimics 'just enough' Postgres.

    For each connection it:

      - Reads the StartupMessage (length-prefixed).
      - Sends an ``AuthenticationOk`` (R, length 8, int32 0) plus a
        ``ReadyForQuery`` (Z, length 5, byte 'I') so a real psql
        client would think it had a session.
      - Records every subsequent client message so tests can assert
        the proxy forwarded what it should have forwarded.
    """

    def __init__(self) -> None:
        self.received_messages: list[bytes] = []
        self.connections: int = 0
        self._server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0  # set after start()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        socks = self._server.sockets
        assert socks, "fake upstream did not bind"
        self.host, self.port = socks[0].getsockname()[:2]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            # StartupMessage: 4-byte length (includes itself).
            head = await reader.readexactly(4)
            (length,) = struct.unpack(">I", head)
            await reader.readexactly(length - 4)
            # Send AuthenticationOk + ReadyForQuery.
            writer.write(b"R" + struct.pack(">II", 8, 0))
            writer.write(b"Z" + struct.pack(">I", 5) + b"I")
            await writer.drain()
            # Record everything else the client sends until EOF.
            while True:
                try:
                    type_byte = await reader.readexactly(1)
                    length_bytes = await reader.readexactly(4)
                    (length,) = struct.unpack(">I", length_bytes)
                    payload = await reader.readexactly(length - 4)
                    self.received_messages.append(type_byte + length_bytes + payload)
                    # Reply with a CommandComplete + ReadyForQuery so
                    # tests that drive a "happy path" through the proxy
                    # see a recognizable response. (For Q type only.)
                    if type_byte == b"Q":
                        tag = b"SELECT 0\x00"
                        writer.write(b"C" + struct.pack(">I", 4 + len(tag)) + tag)
                        writer.write(b"Z" + struct.pack(">I", 5) + b"I")
                        await writer.drain()
                except asyncio.IncompleteReadError:
                    return
        except Exception:  # pragma: no cover - the fake upstream stays quiet on cleanup
            return
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers for raw PG wire bytes
# ---------------------------------------------------------------------------


def _build_startup(params: dict[str, str]) -> bytes:
    """Encode a v3.0 StartupMessage with the given key/value pairs."""
    body = struct.pack(">I", 0x00030000)
    for k, v in params.items():
        body += k.encode("utf-8") + b"\x00" + v.encode("utf-8") + b"\x00"
    body += b"\x00"  # final terminator
    length = 4 + len(body)
    return struct.pack(">I", length) + body


def _build_simple_query(sql: str) -> bytes:
    payload = sql.encode("utf-8") + b"\x00"
    length = 4 + len(payload)
    return b"Q" + struct.pack(">I", length) + payload


async def _read_one_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read one (type, payload) tuple from a stream."""
    type_byte = await reader.readexactly(1)
    length_bytes = await reader.readexactly(4)
    (length,) = struct.unpack(">I", length_bytes)
    payload = await reader.readexactly(length - 4)
    return type_byte, payload


# ---------------------------------------------------------------------------
# Wire-protocol unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream() -> Any:
    """A running fake upstream PG."""
    server = _FakeUpstream()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _start_proxy(
    upstream: _FakeUpstream,
    charter: Charter | None,
    *,
    fetch_error: Exception | None = None,
    hits_grader: Any = None,
) -> tuple[CharterGatedProxy, asyncio.AbstractServer, int]:
    """Spin up a CharterGatedProxy bound to an ephemeral port.

    The fetcher is injected as a closure so we never hit the network.
    """

    def fetcher(_url: str) -> Charter:
        if fetch_error is not None:
            raise fetch_error
        assert charter is not None
        return charter

    proxy = CharterGatedProxy(
        bind_host="127.0.0.1",
        bind_port=0,
        upstream_host=upstream.host,
        upstream_port=upstream.port,
        hits_grader=hits_grader,
        fetch_charter_fn=fetcher,
    )
    server = await proxy.start_server()
    socks = server.sockets
    assert socks, "proxy did not bind"
    bound_port = socks[0].getsockname()[1]
    return proxy, server, bound_port


def _parse_error_response(payload: bytes) -> dict[str, str]:
    """Decode an ErrorResponse payload into a ``{field_code: value}`` dict."""
    fields: dict[str, str] = {}
    i = 0
    while i < len(payload) and payload[i] != 0:
        code = chr(payload[i])
        i += 1
        end = payload.find(b"\x00", i)
        if end == -1:
            break
        fields[code] = payload[i:end].decode("utf-8", errors="replace")
        i = end + 1
    return fields


async def test_error_response_round_trip() -> None:
    """The ErrorResponse builder produces bytes our parser understands."""
    raw = _build_error_response("hello", code="42501", severity="ERROR")
    assert raw.startswith(b"E")
    (length,) = struct.unpack(">I", raw[1:5])
    assert length == len(raw) - 1
    fields = _parse_error_response(raw[5:])
    assert fields == {"S": "ERROR", "C": "42501", "M": "hello"}


async def test_missing_charter_url_is_refused(upstream: _FakeUpstream) -> None:
    """StartupMessage without charter_url → ErrorResponse, no upstream."""
    charter = _build_charter()
    _, server, port = await _start_proxy(upstream, charter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(_build_startup({"user": "alice", "database": "demo"}))
        await writer.drain()

        type_byte, payload = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte == b"E"
        fields = _parse_error_response(payload)
        assert fields.get("C") == "42501"
        assert "charter_url required" in fields.get("M", "")
        # The fake upstream should never see this connection.
        assert upstream.connections == 0

        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_charter_fetch_failure_is_refused(upstream: _FakeUpstream) -> None:
    """Charter fetch raising → fail-closed, no upstream connection."""
    _, server, port = await _start_proxy(
        upstream,
        charter=None,
        fetch_error=CharterNotFoundError("GET ... -> HTTP 404"),
    )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            _build_startup(
                {
                    "user": "alice",
                    "database": "demo",
                    "charter_url": "http://example.invalid/charter",
                }
            )
        )
        await writer.drain()

        type_byte, payload = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte == b"E"
        fields = _parse_error_response(payload)
        assert fields.get("C") == "42501"
        # The reason should mention the underlying error class so
        # operators can debug.
        assert "CharterNotFoundError" in fields.get("M", "")
        assert upstream.connections == 0

        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_incompatible_query_is_refused(upstream: _FakeUpstream) -> None:
    """A DROP TABLE under the default Charter → refused, not forwarded.

    The default Charter has an ``out_of_scope`` clause and the default
    grader marks every clause as hit, so the aggregate is
    ``incompatible``. The proxy MUST NOT forward the Q message
    upstream.
    """
    charter = _build_charter()
    _, server, port = await _start_proxy(upstream, charter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            _build_startup(
                {
                    "user": "alice",
                    "database": "demo",
                    "charter_url": "http://example.invalid/charter",
                }
            )
        )
        await writer.drain()

        # Drain the upstream's AuthenticationOk + ReadyForQuery that
        # were forwarded to us.
        await asyncio.wait_for(_read_one_message(reader), timeout=2.0)  # R
        await asyncio.wait_for(_read_one_message(reader), timeout=2.0)  # Z

        # Send the offending Q.
        writer.write(_build_simple_query("DROP TABLE production"))
        await writer.drain()

        type_byte, payload = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte == b"E"
        fields = _parse_error_response(payload)
        assert fields.get("C") == "42501"
        assert "incompatible" in fields.get("M", "").lower()

        # ReadyForQuery should follow so the client knows it can try
        # another statement.
        type_byte2, _ = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte2 == b"Z"

        # The upstream must NOT have received the DROP. (It will have
        # received the StartupMessage forwarded earlier, plus possibly
        # nothing else.)
        forwarded_types = [m[:1] for m in upstream.received_messages]
        assert b"Q" not in forwarded_types

        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_allow_query_is_forwarded(upstream: _FakeUpstream) -> None:
    """An allow-verdict Q must reach the upstream verbatim.

    To get an ``allow`` verdict we build a Charter with only a
    permissive ``scope`` clause and rely on the conservative default
    grader: ``scope`` maps to ``allow`` in TYPE_TO_DECISION, and with
    no restrictive clause in the Charter the aggregate is ``allow``.
    """
    charter = _build_charter(out_of_scope_text=None)
    _, server, port = await _start_proxy(upstream, charter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            _build_startup(
                {
                    "user": "alice",
                    "database": "demo",
                    "charter_url": "http://example.invalid/charter",
                }
            )
        )
        await writer.drain()

        await asyncio.wait_for(_read_one_message(reader), timeout=2.0)  # R
        await asyncio.wait_for(_read_one_message(reader), timeout=2.0)  # Z

        writer.write(_build_simple_query("SELECT 1"))
        await writer.drain()

        # Allow path: the fake upstream replies with CommandComplete +
        # ReadyForQuery, which the proxy forwards back to us.
        type_byte, _ = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte == b"C"
        type_byte2, _ = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte2 == b"Z"

        # The fake upstream should now have a Q in its received list.
        forwarded_types = [m[:1] for m in upstream.received_messages]
        assert b"Q" in forwarded_types

        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_unparseable_sql_fails_closed(upstream: _FakeUpstream) -> None:
    """Garbage SQL → fail-closed refusal even if charter would allow.

    Even with a permissive Charter, ``intent_from_sql`` falling into
    the ``OTHER`` branch should drive the gate to refuse — this is
    the safety net the issue explicitly calls out.
    """
    charter = _build_charter(out_of_scope_text=None)
    _, server, port = await _start_proxy(upstream, charter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            _build_startup(
                {
                    "user": "alice",
                    "database": "demo",
                    "charter_url": "http://example.invalid/charter",
                }
            )
        )
        await writer.drain()

        await asyncio.wait_for(_read_one_message(reader), timeout=2.0)  # R
        await asyncio.wait_for(_read_one_message(reader), timeout=2.0)  # Z

        writer.write(_build_simple_query("not even valid sql {{{ }"))
        await writer.drain()

        type_byte, payload = await asyncio.wait_for(_read_one_message(reader), timeout=2.0)
        assert type_byte == b"E"
        fields = _parse_error_response(payload)
        assert fields.get("C") == "42501"

        forwarded_types = [m[:1] for m in upstream.received_messages]
        assert b"Q" not in forwarded_types, (
            f"Unparseable SQL must not be forwarded; got {forwarded_types!r}"
        )

        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_default_grader_marks_every_clause_hit() -> None:
    """The default grader's contract: every clause hit at full confidence."""
    charter = _build_charter()
    hits = _default_hits_grader(charter, "any task")
    assert [h["id"] for h in hits] == ["C-001", "C-002"]
    assert all(h["hit"] is True for h in hits)
    assert all(h["confidence"] == 1.0 for h in hits)


# ---------------------------------------------------------------------------
# Real-Postgres integration test (optional)
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
async def test_real_postgres_end_to_end() -> None:
    """End-to-end smoke against a real ephemeral Postgres.

    Skipped automatically when ``testcontainers`` or its docker prerequisite
    is unavailable — this is a teaching reference, not a CI must-pass.
    Run locally with ``pip install testcontainers asyncpg`` and a working
    docker daemon to exercise the path.
    """
    try:
        testcontainers_pg = importlib.import_module("testcontainers.postgres")
        asyncpg = importlib.import_module("asyncpg")
    except ImportError as e:  # pragma: no cover
        pytest.skip(f"testcontainers / asyncpg not installed: {e}")

    pg_container_cls = testcontainers_pg.PostgresContainer
    try:
        container = pg_container_cls("postgres:16-alpine")
        container.start()
    except Exception as e:  # pragma: no cover - docker missing
        pytest.skip(f"docker / Postgres container unavailable: {e}")

    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        user = container.username
        password = container.password
        database = container.dbname

        # Seed a public table the gated charter allows.
        bootstrap = await asyncpg.connect(
            host=host, port=port, user=user, password=password, database=database
        )
        await bootstrap.execute(
            "CREATE TABLE public_reports (id int primary key, label text); "
            "INSERT INTO public_reports VALUES (1, 'ok');"
        )
        await bootstrap.close()

        charter = _build_charter(
            scope_text="Read rows from public_reports for analytics.",
            out_of_scope_text=None,
        )
        proxy, server, proxy_port = await _start_proxy(_StaticEndpoint(host, port), charter)
        try:
            conn = await asyncpg.connect(
                host="127.0.0.1",
                port=proxy_port,
                user=user,
                password=password,
                database=database,
                server_settings={"charter_url": "http://example.invalid/charter"},
            )
            rows = await conn.fetch("SELECT id, label FROM public_reports")
            assert [dict(r) for r in rows] == [{"id": 1, "label": "ok"}]
            await conn.close()
        finally:
            server.close()
            await server.wait_closed()
    finally:
        with contextlib.suppress(Exception):
            container.stop()


class _StaticEndpoint:
    """Tiny shim so ``_start_proxy`` can be reused with a real PG host."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.received_messages: list[bytes] = []
        self.connections = 0
