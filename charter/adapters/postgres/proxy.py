"""Asyncio TCP proxy that gates Postgres SQL on a Charter verdict.

Reference implementation, NOT a production database front. The minimum
slice of the PG wire protocol needed to ship a teaching sample:

  - Read the client's ``StartupMessage`` (the only message that has no
    one-byte type prefix), pluck the non-standard ``charter_url``
    parameter out of the NUL-terminated key/value pairs, forward the
    message verbatim to the upstream Postgres.
  - Forward every subsequent client message in both directions
    transparently, **except** that ``Q`` (Simple Query) and ``P``
    (Parse) messages get their SQL payload extracted and fed through
    :func:`charter.adapters.postgres.gate.check`.
  - If the verdict is not ``allow``, the proxy refuses: it does NOT
    forward the message upstream, it writes a PG ``ErrorResponse`` to
    the client (SQLSTATE 42501 — ``insufficient_privilege``), then a
    ``ReadyForQuery`` so the client can recover and try another
    statement, then continues looping.

Wire-format details we rely on:

  - ``StartupMessage``: 4-byte length (big-endian, includes itself) +
    4-byte protocol version (0x0003_0000 for v3) + a list of NUL-
    terminated key/value pairs, terminated by an extra NUL byte.
  - Every other message: 1-byte type + 4-byte length (big-endian,
    includes the length bytes but NOT the type byte) + payload.
  - ``Q`` payload: SQL string + trailing NUL.
  - ``P`` payload: name (NUL) + SQL (NUL) + parameter count (int16) +
    that many parameter OIDs (int32).
  - ``E`` (ErrorResponse) payload: a sequence of one-byte field codes
    each followed by a NUL-terminated string, terminated by a final
    NUL byte. We use ``S`` (severity), ``C`` (SQLSTATE), ``M``
    (message).

Wire references: PostgreSQL Protocol Flow chapter (the only
authoritative source). We intentionally do NOT depend on a wire
library; the parser is ~30 lines and pulling in ``pgproto`` etc.
would mask how the message stream actually looks.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
from collections.abc import Awaitable, Callable

from ..._logging import get_logger
from ...errors import CharterError
from ...mcp_server import _fetch_and_verify
from ...schema import Charter
from .gate import HitsGrader, check
from .intent import intent_from_sql

_log = get_logger("charter.adapters.postgres.proxy")

# Type aliases keep the constructor signature readable. The Charter
# fetcher is injectable so tests can avoid network calls; the default
# delegates to `_fetch_and_verify`, which is the same primitive every
# other adapter uses (so the trust order signature → JWKS → pin →
# lifecycle is identical across adapters).
FetchCharterFn = Callable[[str], Charter]

# Default PG protocol limits. We cap reads at 1 MiB per message to
# block trivial OOM attempts (a client claiming a 2 GiB Query payload
# would otherwise pin a buffer). 1 MiB comfortably accommodates the
# largest realistic ad-hoc query.
_MAX_MESSAGE_BYTES = 1 * 1024 * 1024

# Protocol version 3.0 = 0x0003_0000. Anything else (SSLRequest =
# 0x04D2_162F, CancelRequest = 0x04D2_162E, GSSENCRequest = 0x04D2_1630)
# we refuse: the reference adapter does not negotiate TLS or cancel.
_PROTOCOL_V3 = 0x00030000


class ProxyError(Exception):
    """Internal sentinel raised by the wire helpers.

    The connection handler catches it, logs the cause, and writes a
    PG ``ErrorResponse`` to the client before closing. Surfaced as a
    distinct type so legitimate ``asyncio`` errors are not silently
    confused with malformed-protocol cases.
    """


def _build_error_response(
    message: str,
    *,
    code: str = "42501",
    severity: str = "ERROR",
) -> bytes:
    """Encode a PG ``ErrorResponse`` message.

    Fields:
        S — severity (always ``ERROR`` for refusals; the wire spec
            also permits ``FATAL`` / ``PANIC`` / ``WARNING`` etc.)
        C — SQLSTATE code, 5 chars. Default ``42501`` is
            ``insufficient_privilege`` from the PG error class table —
            the closest documented match for "your authority does not
            permit this operation".
        M — human-readable message. Truncated to 4 KiB defensively.

    Returns the full wire bytes (type byte + length-prefixed payload).
    """
    if len(message) > 4096:
        message = message[:4093] + "..."
    # Per PG wire spec: each field is `<one-byte type code><NUL-terminated
    # value>`; the whole list is terminated by a final NUL byte.
    body = b"S" + severity.encode("utf-8") + b"\x00"
    body += b"C" + code.encode("utf-8") + b"\x00"
    body += b"M" + message.encode("utf-8") + b"\x00"
    body += b"\x00"  # final terminator
    length = 4 + len(body)
    return b"E" + struct.pack(">I", length) + body


def _build_ready_for_query(status: bytes = b"I") -> bytes:
    """Encode a ``ReadyForQuery`` message.

    Status byte is ``I`` (idle), ``T`` (in transaction), or ``E``
    (failed transaction). The proxy always reports ``I`` after a
    refusal so the client knows it can issue another statement —
    we never had a real transaction to be inside of.
    """
    return b"Z" + struct.pack(">I", 5) + status


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise :class:`ProxyError`.

    ``StreamReader.readexactly`` is the standard helper; we wrap it
    so the EOF / incomplete-read cases land in our error-classifying
    branch rather than propagating ``IncompleteReadError`` to the
    asyncio top level.
    """
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as e:
        raise ProxyError(f"client closed mid-message after {len(e.partial)} bytes") from e


def _parse_startup_params(payload: bytes) -> dict[str, str]:
    """Parse NUL-terminated key/value pairs out of a StartupMessage.

    The payload (after stripping the 4-byte protocol version) is a
    sequence of ``key\\0value\\0`` pairs, terminated by a single extra
    NUL byte. Decode each pair as UTF-8; reject malformed encoding
    rather than silently dropping pairs.
    """
    # Strip all trailing NULs in one go. The wire shape is
    # ``key\0value\0...key\0value\0\0`` (note the final terminator NUL
    # after the last value's own NUL); some encoders emit only a
    # single trailing NUL, so we accept both by stripping until none
    # remain. After stripping, the payload is a clean
    # ``key\0value\0key\0value`` (no trailing NUL) which splits into
    # exactly 2N tokens.
    while payload.endswith(b"\x00"):
        payload = payload[:-1]

    if not payload:
        return {}

    parts = payload.split(b"\x00")
    if len(parts) % 2 != 0:
        raise ProxyError(f"StartupMessage parameters not in key/value pairs ({len(parts)} tokens)")
    out: dict[str, str] = {}
    try:
        for i in range(0, len(parts), 2):
            key = parts[i].decode("utf-8")
            value = parts[i + 1].decode("utf-8")
            if key:
                out[key] = value
    except UnicodeDecodeError as e:
        raise ProxyError(f"StartupMessage parameter not valid UTF-8: {e}") from e
    return out


async def _read_startup(reader: asyncio.StreamReader) -> tuple[bytes, dict[str, str]]:
    """Read the client's StartupMessage and return ``(raw_bytes, params)``.

    The raw bytes are needed because we still forward the message to
    the upstream Postgres verbatim — we are an audit checkpoint, not a
    protocol translator. The parsed params are needed because we pull
    ``charter_url`` out of them.
    """
    length_bytes = await _read_exact(reader, 4)
    (length,) = struct.unpack(">I", length_bytes)
    if length < 8 or length > _MAX_MESSAGE_BYTES:
        raise ProxyError(f"StartupMessage length {length} out of bounds")
    rest = await _read_exact(reader, length - 4)
    raw = length_bytes + rest

    (proto_version,) = struct.unpack(">I", rest[:4])
    if proto_version != _PROTOCOL_V3:
        # Could be SSLRequest / CancelRequest / GSSENCRequest. The
        # reference proxy does not implement any of them; tell the
        # client clearly. Returning a single 'N' byte (the standard
        # PG response to SSLRequest meaning "no SSL") would be a more
        # cooperative answer for SSLRequest specifically, but the
        # reference adapter is explicitly cleartext-only and we'd
        # rather fail loud than half-implement TLS negotiation.
        raise ProxyError(
            f"unsupported startup protocol version 0x{proto_version:08x} "
            "(reference proxy only handles PG protocol v3)"
        )
    params = _parse_startup_params(rest[4:])
    return raw, params


def _extract_query_sql(payload: bytes) -> str:
    """Decode a Simple Query (``Q``) payload's SQL string.

    Payload layout: SQL bytes + trailing NUL. We strip the NUL and
    decode UTF-8 strictly; malformed encoding → ``ProxyError`` →
    fail-closed refusal at the call site.
    """
    if not payload.endswith(b"\x00"):
        raise ProxyError("Query payload missing trailing NUL")
    try:
        return payload[:-1].decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProxyError(f"Query SQL not valid UTF-8: {e}") from e


def _extract_parse_sql(payload: bytes) -> str:
    """Decode a Parse (``P``) payload's SQL string.

    Payload layout: statement-name (NUL) + SQL (NUL) + int16 param
    count + param-count * int32 OIDs. We only need the SQL string,
    which sits between the first two NUL bytes.
    """
    first = payload.find(b"\x00")
    if first < 0:
        raise ProxyError("Parse payload missing statement-name terminator")
    second = payload.find(b"\x00", first + 1)
    if second < 0:
        raise ProxyError("Parse payload missing SQL terminator")
    try:
        return payload[first + 1 : second].decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProxyError(f"Parse SQL not valid UTF-8: {e}") from e


async def _send_refusal(
    writer: asyncio.StreamWriter,
    message: str,
    *,
    code: str = "42501",
) -> None:
    """Write an ErrorResponse + ReadyForQuery and flush.

    The double-message pattern matches what real PG does on a failed
    statement: error first so the client surfaces the cause, then
    ReadyForQuery so the client knows it can keep going. We always
    use status ``I`` (idle) because the proxy is not actually inside
    a transaction.
    """
    writer.write(_build_error_response(message, code=code))
    writer.write(_build_ready_for_query(b"I"))
    # Client gave up between message and drain — nothing to do.
    with contextlib.suppress(ConnectionResetError, BrokenPipeError):
        await writer.drain()


class CharterGatedProxy:
    """asyncio TCP proxy that gates SQL on a Charter compatibility verdict.

    Construction is total; nothing happens until :meth:`serve` is
    awaited. The proxy is single-process and stateless; each accepted
    client connection runs as its own task and owns its own upstream
    connection (no pooling — this is a reference implementation).

    Args:
        bind_host / bind_port:
            Listening address for clients. Defaults read the
            ``CHARTER_PG_PROXY_BIND`` env var (``host:port`` form),
            falling back to ``127.0.0.1:55432``.
        upstream_host / upstream_port:
            Real PG to forward approved traffic to. Defaults from
            ``CHARTER_PG_PROXY_UPSTREAM`` (``host:port``), falling
            back to ``127.0.0.1:5432``.
        hits_grader:
            Injectable per-clause grader. Default is the conservative
            "all clauses hit, confidence 1.0" stub
            (:func:`gate._default_hits_grader`).
        fetch_charter_fn:
            Injectable Charter fetcher. Default delegates to
            :func:`charter.mcp_server._fetch_and_verify` so trust
            order (signature → JWKS → pin → lifecycle) matches every
            other adapter.
    """

    def __init__(
        self,
        *,
        bind_host: str | None = None,
        bind_port: int | None = None,
        upstream_host: str | None = None,
        upstream_port: int | None = None,
        hits_grader: HitsGrader | None = None,
        fetch_charter_fn: FetchCharterFn = _fetch_and_verify,
    ) -> None:
        bh, bp = _resolve_endpoint(
            override_host=bind_host,
            override_port=bind_port,
            env_var="CHARTER_PG_PROXY_BIND",
            default=("127.0.0.1", 55432),
        )
        uh, up = _resolve_endpoint(
            override_host=upstream_host,
            override_port=upstream_port,
            env_var="CHARTER_PG_PROXY_UPSTREAM",
            default=("127.0.0.1", 5432),
        )
        self.bind_host = bh
        self.bind_port = bp
        self.upstream_host = uh
        self.upstream_port = up
        self.hits_grader = hits_grader
        self.fetch_charter_fn = fetch_charter_fn

    # -- public surface ----------------------------------------------------

    async def serve(self) -> None:
        """Bind the listening socket and serve forever.

        This is the entry point used by the ``charter-pg-proxy``
        console script. Tests typically use :meth:`start_server`
        instead so they can stop the server cleanly between cases.
        """
        server = await self.start_server()
        async with server:
            await server.serve_forever()

    async def start_server(self) -> asyncio.AbstractServer:
        """Start the listener and return the unstarted ``Server`` object.

        Useful in tests: they can grab ``server.sockets[0].getsockname()``
        to discover the bound port (when ``bind_port=0``) and then
        ``server.close()`` / ``server.wait_closed()`` on teardown.
        """
        server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_host,
            port=self.bind_port,
        )
        _log.info(
            "charter-pg-proxy listening",
            extra={
                "bind": f"{self.bind_host}:{self.bind_port}",
                "upstream": f"{self.upstream_host}:{self.upstream_port}",
            },
        )
        return server

    # -- per-connection handler -------------------------------------------

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Per-connection entrypoint registered with ``asyncio.start_server``.

        Steps:
          1. Read the StartupMessage; pluck ``charter_url`` out of the
             parameters.
          2. Fetch + verify the Charter. Any failure → refuse and
             close, before connecting upstream. (No upstream FD is
             ever allocated for a client that fails the Charter
             pre-check — avoids accidentally exposing the upstream
             to a malformed client.)
          3. Open the upstream connection and forward the
             StartupMessage verbatim.
          4. Spawn the upstream → client copy task (no inspection) and
             the client → upstream loop (inspects every message,
             gates ``Q``/``P``). Run them concurrently until either
             side closes.
        """
        peer = client_writer.get_extra_info("peername")
        try:
            startup_raw, params = await _read_startup(client_reader)
        except ProxyError as e:
            _log.warning(
                "startup read failed",
                extra={"peer": str(peer), "error": str(e), "outcome": "incompatible"},
            )
            await _send_refusal(
                client_writer,
                f"Charter proxy: malformed StartupMessage ({e})",
                code="08P01",  # protocol_violation
            )
            await _close(client_writer)
            return

        charter_url = params.get("charter_url")
        if not charter_url:
            _log.warning(
                "startup missing charter_url",
                extra={"peer": str(peer), "outcome": "incompatible"},
            )
            await _send_refusal(
                client_writer,
                "Charter proxy: charter_url required as a startup parameter; refusing.",
            )
            await _close(client_writer)
            return

        try:
            charter = await asyncio.to_thread(self.fetch_charter_fn, charter_url)
        except CharterError as e:
            _log.warning(
                "charter fetch failed",
                extra={
                    "peer": str(peer),
                    "charter_url": charter_url,
                    "error": f"{type(e).__name__}: {e}",
                    "outcome": "incompatible",
                },
            )
            await _send_refusal(
                client_writer,
                f"Charter proxy: failed to fetch/verify charter at {charter_url}: "
                f"{type(e).__name__}: {e}",
            )
            await _close(client_writer)
            return
        except Exception as e:
            # Caller-injected fetchers may raise anything. Per the
            # fail-closed contract we MUST NOT let those propagate.
            _log.exception(
                "charter fetch raised unexpected",
                extra={
                    "peer": str(peer),
                    "charter_url": charter_url,
                    "error": f"{type(e).__name__}: {e}",
                    "outcome": "incompatible",
                },
            )
            await _send_refusal(
                client_writer,
                f"Charter proxy: charter fetcher raised {type(e).__name__}: {e}",
            )
            await _close(client_writer)
            return

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                host=self.upstream_host,
                port=self.upstream_port,
            )
        except OSError as e:
            _log.error(
                "upstream connection failed",
                extra={
                    "peer": str(peer),
                    "upstream": f"{self.upstream_host}:{self.upstream_port}",
                    "error": str(e),
                    "outcome": "incompatible",
                },
            )
            await _send_refusal(
                client_writer,
                f"Charter proxy: cannot reach upstream Postgres ({e})",
            )
            await _close(client_writer)
            return

        _log.info(
            "client charter ok, forwarding",
            extra={
                "peer": str(peer),
                "charter_id": charter.charter_id,
                "principal_id": charter.binding.principal_id,
                "agent_id": charter.binding.agent_id,
                "outcome": "ok",
            },
        )

        # Forward the StartupMessage verbatim. We do NOT strip the
        # `charter_url` parameter because Postgres ignores unknown
        # startup parameters; leaving it in is harmless and keeps the
        # forwarded byte stream identical to what we received.
        upstream_writer.write(startup_raw)
        try:
            await upstream_writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            await _close(client_writer)
            await _close(upstream_writer)
            return

        # Pump in both directions concurrently. Upstream → client is
        # transparent; client → upstream filters Q/P.
        upstream_to_client = asyncio.create_task(
            _pump_transparent(upstream_reader, client_writer),
            name="pg-proxy-upstream-to-client",
        )
        client_to_upstream = asyncio.create_task(
            self._pump_client_to_upstream(
                client_reader,
                client_writer,
                upstream_writer,
                charter,
            ),
            name="pg-proxy-client-to-upstream",
        )

        # When either direction ends, tear the other down so we do not
        # leak a half-open socket.
        done, pending = await asyncio.wait(
            {upstream_to_client, client_to_upstream},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            # Awaiting a cancelled / errored pump task is purely for
            # cleanup; we never re-raise here. Either CancelledError
            # (the normal case) or any pump-internal exception is
            # already logged via the `done` task loop below.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None:
                _log.debug(
                    "pump task ended with exception",
                    extra={"task": task.get_name(), "error": repr(exc)},
                )

        await _close(client_writer)
        await _close(upstream_writer)

    async def _pump_client_to_upstream(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_writer: asyncio.StreamWriter,
        charter: Charter,
    ) -> None:
        """Forward client → upstream, intercepting Query and Parse."""
        while not client_reader.at_eof():
            try:
                type_byte = await client_reader.readexactly(1)
            except asyncio.IncompleteReadError:
                return  # clean EOF
            try:
                length_bytes = await _read_exact(client_reader, 4)
            except ProxyError:
                return
            (length,) = struct.unpack(">I", length_bytes)
            if length < 4 or length > _MAX_MESSAGE_BYTES:
                _log.warning(
                    "oversized client message; closing",
                    extra={
                        "length": length,
                        "type": type_byte.decode("ascii", errors="replace"),
                        "outcome": "incompatible",
                    },
                )
                await _send_refusal(
                    client_writer,
                    f"Charter proxy: client message length {length} out of bounds; "
                    "closing connection.",
                    code="08P01",
                )
                return
            payload = await _read_exact(client_reader, length - 4)
            raw_message = type_byte + length_bytes + payload

            if type_byte in (b"Q", b"P"):
                try:
                    sql = (
                        _extract_query_sql(payload)
                        if type_byte == b"Q"
                        else _extract_parse_sql(payload)
                    )
                except ProxyError as e:
                    _log.warning(
                        "malformed Q/P payload; refusing",
                        extra={
                            "type": type_byte.decode("ascii"),
                            "error": str(e),
                            "outcome": "incompatible",
                        },
                    )
                    await _send_refusal(
                        client_writer,
                        f"Charter proxy: malformed {type_byte.decode('ascii')} message ({e})",
                    )
                    continue

                intent = intent_from_sql(sql)
                try:
                    verdict = await asyncio.to_thread(check, intent, charter, self.hits_grader)
                except Exception as e:
                    # Belt-and-suspenders: gate.check is supposed to
                    # catch every grader/aggregate exception itself, but
                    # if something slips through we still must not
                    # forward the SQL.
                    _log.exception(
                        "gate.check raised; refusing fail-closed",
                        extra={
                            "operation": intent.operation,
                            "tables": intent.tables,
                            "error": f"{type(e).__name__}: {e}",
                            "outcome": "incompatible",
                        },
                    )
                    await _send_refusal(
                        client_writer,
                        f"Charter proxy: gate raised {type(e).__name__}: {e}; refused.",
                    )
                    continue

                if verdict.decision != "allow":
                    applied = ", ".join(m.id for m in verdict.matched_clauses if m.applied)
                    msg = (
                        f"Charter proxy: verdict={verdict.decision} "
                        f"(clauses applied: {applied or 'none'}). "
                        f"{verdict.reason}"
                    )
                    _log.info(
                        "refused SQL",
                        extra={
                            "operation": intent.operation,
                            "tables": intent.tables,
                            "has_pii": intent.has_pii_columns,
                            "charter_id": charter.charter_id,
                            "decision": verdict.decision,
                            "outcome": verdict.decision,
                        },
                    )
                    await _send_refusal(client_writer, msg)
                    continue

                _log.info(
                    "allowed SQL",
                    extra={
                        "operation": intent.operation,
                        "tables": intent.tables,
                        "has_pii": intent.has_pii_columns,
                        "charter_id": charter.charter_id,
                        "decision": verdict.decision,
                        "outcome": "ok",
                    },
                )

            # Everything else (or allow-verdict Q/P): forward verbatim.
            upstream_writer.write(raw_message)
            try:
                await upstream_writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return


async def _pump_transparent(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
) -> None:
    """One-way byte copy with no inspection.

    Used for the upstream → client direction. We use a generous read
    chunk (32 KiB) so the proxy does not Nagle-style fragment large
    server responses (rows, error packets, NOTICE blobs, ...).
    """
    try:
        while True:
            chunk = await src.read(32 * 1024)
            if not chunk:
                return
            dst.write(chunk)
            try:
                await dst.drain()
            except (ConnectionResetError, BrokenPipeError):
                return
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return


async def _close(writer: asyncio.StreamWriter) -> None:
    """Close a writer without raising on already-closed sockets."""
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


def _resolve_endpoint(
    *,
    override_host: str | None,
    override_port: int | None,
    env_var: str,
    default: tuple[str, int],
) -> tuple[str, int]:
    """Pick (host, port) from explicit args > env var > default.

    The env var accepts a single ``host:port`` string. We intentionally
    don't merge host from one source and port from another — too easy
    to produce surprising configurations.
    """
    if override_host is not None and override_port is not None:
        return override_host, override_port
    raw = os.environ.get(env_var)
    if raw:
        host, _, port_s = raw.rpartition(":")
        if host and port_s.isdigit():
            return host, int(port_s)
    return default


def run() -> None:  # pragma: no cover - thin CLI entrypoint
    """Console-script entrypoint: ``charter-pg-proxy``.

    No arg parsing; the proxy is configured entirely via env vars so
    tooling stays declarative (docker compose, systemd units, ...).
    For ad-hoc use, set ``CHARTER_PG_PROXY_BIND`` and
    ``CHARTER_PG_PROXY_UPSTREAM`` on the invocation line.
    """
    proxy = CharterGatedProxy()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(proxy.serve())


__all__ = [
    "CharterGatedProxy",
    "FetchCharterFn",
    "run",
]


# Awaitable type alias kept for future symmetry with the AP2 adapter's
# async-fetcher path (currently only the synchronous variant is in use,
# but exposing the alias documents the intent).
AsyncFetchCharterFn = Callable[[str], Awaitable[Charter]]
