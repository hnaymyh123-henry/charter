"""Revocation propagation (B1.3) — pull-mode visibility into revoked Charters.

`charter revoke` flips the live Charter's `lifecycle.status` to `"revoked"`
and re-signs, but a calling agent that already cached the previous fetch
will keep trusting the old verdict until its cache TTL expires. This
module is the protocol-level fix: a poll-mode revocation feed plus an
SDK helper that auto-evicts cached entries.

## Where the truth lives

Revocation state is **derived** — there is no separate persistence file.
The transparency log records every signed Charter (idempotent on
`charter_id`, so revoke / renew re-signs do NOT duplicate entries). The
revoked-ness lives on the Charter file itself (`lifecycle.status`,
`lifecycle.revoked_at`). To enumerate revoked Charters we walk the
transparency log and, for each entry, load the live or archived Charter
and check its status. This matches ADR-007 ("revocation info goes
through the transparency log, not a second source of truth") and
ADR-001 (no database — pure file + NDJSON stream).

## Server side

`iter_revoked_entries(since)` walks the log and yields one
`RevocationEntry` per entry with `seq > since` whose Charter file is
revoked. The server's `GET /transparency/revoked` endpoint streams these
as `application/x-ndjson`.

## Client side

`subscribe_revocations(origin, since, *, poll_interval=60)` is an async
generator that periodically GETs the endpoint and yields each new entry.
`RevocationAwareCache` wraps a `dict[charter_id, Charter]` and runs a
background task that evicts matching entries.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from . import transparency
from ._logging import get_logger
from .schema import Charter
from .storage import load_archived_charter, load_charter

if TYPE_CHECKING:
    from types import TracebackType

_log = get_logger("charter.revocation")


# ---------------------------------------------------------------------------
# RevocationEntry — the on-the-wire shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevocationEntry:
    """One revoked Charter, as emitted by `/transparency/revoked`.

    `seq` is the transparency-log sequence of the entry's *issuance* —
    revoke / renew re-signs are idempotent on `charter_id`, so the
    original seq stays stable. Clients use it as the cursor for the next
    `?since=` pull.
    """

    charter_id: str
    principal_id: str
    agent_id: str
    revoked_at: datetime
    seq: int

    def to_dict(self) -> dict[str, object]:
        return {
            "charter_id": self.charter_id,
            "principal_id": self.principal_id,
            "agent_id": self.agent_id,
            "revoked_at": self.revoked_at.isoformat(),
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> RevocationEntry:
        seq_raw = raw["seq"]
        if not isinstance(seq_raw, int):
            raise ValueError("revocation entry 'seq' must be an int")
        return cls(
            charter_id=str(raw["charter_id"]),
            principal_id=str(raw["principal_id"]),
            agent_id=str(raw["agent_id"]),
            revoked_at=datetime.fromisoformat(str(raw["revoked_at"])),
            seq=seq_raw,
        )


# ---------------------------------------------------------------------------
# Server-side derivation
# ---------------------------------------------------------------------------


def _lookup_charter_for_log_entry(
    charter_id: str, principal_id: str, agent_id: str
) -> Charter | None:
    """Resolve a Charter from a transparency log entry's metadata.

    Tries the live binding first (fastest path; most revoked Charters
    are still at their canonical location with `lifecycle.status =
    "revoked"`). Falls back to the archive, which catches the case where
    the binding was renewed AFTER revoke — predecessor is in
    `data/charters/archive/<safe_charter_id>.json` with the old status
    intact.

    Returns None when neither file exists (e.g. the Charter was logged
    but the file has since been deleted out-of-band).
    """
    live = load_charter(principal_id, agent_id)
    if live is not None and live.charter_id == charter_id:
        return live
    return load_archived_charter(charter_id)


def iter_revoked_entries(since: int = 0) -> Iterator[RevocationEntry]:
    """Yield revocation entries with `seq > since`, oldest-first.

    Walks the transparency log; for each entry whose Charter has
    `lifecycle.status == "revoked"`, yields a `RevocationEntry`.
    Charters whose file cannot be loaded are silently skipped (logged at
    WARN) — the alternative would let one corrupt file break the whole
    stream for every other caller.
    """
    for entry in transparency.read_log():
        if entry.seq <= since:
            continue
        principal_id = entry.binding.get("principal_id", "")
        agent_id = entry.binding.get("agent_id", "")
        charter = _lookup_charter_for_log_entry(entry.charter_id, principal_id, agent_id)
        if charter is None:
            _log.warning(
                "revocation scan: charter file missing for logged entry",
                extra={
                    "charter_id": entry.charter_id,
                    "seq": entry.seq,
                    "outcome": "missing_file",
                },
            )
            continue
        if charter.lifecycle.status != "revoked":
            continue
        revoked_at = charter.lifecycle.revoked_at
        if revoked_at is None:
            # Defensive: a revoked Charter is supposed to carry revoked_at,
            # but if a malformed file slipped through we'd rather skip it
            # than emit garbage on the wire.
            _log.warning(
                "revocation scan: revoked charter missing revoked_at",
                extra={
                    "charter_id": entry.charter_id,
                    "seq": entry.seq,
                    "outcome": "missing_revoked_at",
                },
            )
            continue
        yield RevocationEntry(
            charter_id=entry.charter_id,
            principal_id=principal_id,
            agent_id=agent_id,
            revoked_at=revoked_at,
            seq=entry.seq,
        )


# ---------------------------------------------------------------------------
# Client-side subscription
# ---------------------------------------------------------------------------


def _parse_ndjson_entries(body: str) -> Iterator[RevocationEntry]:
    """Parse a `/transparency/revoked` NDJSON response body."""
    import json

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except ValueError:
            _log.warning(
                "revocation subscriber: skipping unparseable line",
                extra={"outcome": "parse_error"},
            )
            continue
        if not isinstance(raw, dict):
            continue
        try:
            yield RevocationEntry.from_dict(raw)
        except (KeyError, ValueError) as e:
            _log.warning(
                "revocation subscriber: skipping malformed entry",
                extra={"error": str(e), "outcome": "schema_error"},
            )


async def subscribe_revocations(
    origin: str,
    since: int = 0,
    *,
    poll_interval: float = 60.0,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[RevocationEntry]:
    """Async generator: poll `{origin}/transparency/revoked?since=...` forever.

    Yields one `RevocationEntry` per new revocation as the cursor
    advances. Tracks the highest seq seen so each poll only requests
    incremental entries — bandwidth grows with revocation rate, not log
    size.

    Args:
        origin: scheme + host (no trailing slash). Example:
            ``"https://charter.example.com"``.
        since: starting cursor. Use 0 on first run; persist the last
            yielded entry's `seq` to resume cleanly across restarts.
        poll_interval: seconds between polls. Default 60 matches the
            Cache-Control TTL on Charter responses — picking it smaller
            does not buy you faster invalidation on a well-behaved
            cache, just more traffic.
        client: optional pre-configured `httpx.AsyncClient`. If omitted
            a fresh client is opened and closed by the generator.

    Cancellation: cancel the consuming task; the generator's `finally`
    closes the owned client (if any). HTTP errors are logged and the
    next poll proceeds — a single 5xx must not kill long-running
    subscribers.
    """
    own_client = client is None
    http = client or httpx.AsyncClient()
    cursor = since
    try:
        while True:
            try:
                resp = await http.get(
                    f"{origin.rstrip('/')}/transparency/revoked",
                    params={"since": cursor},
                )
                resp.raise_for_status()
                for entry in _parse_ndjson_entries(resp.text):
                    if entry.seq > cursor:
                        cursor = entry.seq
                    yield entry
            except httpx.HTTPError as e:
                _log.warning(
                    "revocation subscriber: poll failed; retrying",
                    extra={"origin": origin, "error": str(e), "outcome": "poll_error"},
                )
            await asyncio.sleep(poll_interval)
    finally:
        if own_client:
            await http.aclose()


# ---------------------------------------------------------------------------
# RevocationAwareCache — auto-evicting Charter cache
# ---------------------------------------------------------------------------


class RevocationAwareCache:
    """Dict-shaped Charter cache that auto-evicts on revocation.

    Wraps an in-memory `{charter_id: Charter}` map. On creation (when
    used inside an async context) spawns a background task that polls
    the issuer's `/transparency/revoked` endpoint and `.pop`s any
    cached entry whose `charter_id` arrives in the stream.

    Lifecycle:
      - The polling task is created lazily by ``await cache.start()``,
        or implicitly the first time the cache is used as an async
        context manager.
      - ``await cache.aclose()`` cancels the task cleanly; using the
        cache as ``async with cache: ...`` does this on exit.
      - If the cache is garbage-collected without an explicit close,
        a `weakref.finalize` callback cancels the task. That keeps
        long-running processes from leaking polling tasks but is a
        best-effort fallback — prefer the explicit close.

    Not thread-safe — designed for single-event-loop use.
    """

    def __init__(
        self,
        origin: str,
        *,
        since: int = 0,
        poll_interval: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._origin = origin
        self._poll_interval = poll_interval
        self._client = client
        self._since = since
        self._store: dict[str, Charter] = {}
        self._task: asyncio.Task[None] | None = None
        # `weakref.finalize` is typed with both an owner and a return
        # type in modern stubs. We don't care about either at the API
        # level — the finalize callback is fire-and-forget — so use Any.
        self._finalizer: Any = None

    # --- dict-like surface ----------------------------------------------

    def __getitem__(self, charter_id: str) -> Charter:
        return self._store[charter_id]

    def __setitem__(self, charter_id: str, charter: Charter) -> None:
        self._store[charter_id] = charter

    def __delitem__(self, charter_id: str) -> None:
        del self._store[charter_id]

    def __contains__(self, charter_id: object) -> bool:
        return charter_id in self._store

    def __len__(self) -> int:
        return len(self._store)

    def get(self, charter_id: str, default: Charter | None = None) -> Charter | None:
        return self._store.get(charter_id, default)

    def pop(self, charter_id: str, default: Charter | None = None) -> Charter | None:
        return self._store.pop(charter_id, default)

    # --- async lifecycle ------------------------------------------------

    async def start(self) -> None:
        """Spawn the background polling task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop(), name="charter-revocation-poll")
        # Best-effort cleanup if the user forgets to call aclose().
        self._finalizer = weakref.finalize(self, _cancel_task_if_running, self._task)

    async def aclose(self) -> None:
        """Cancel the polling task and wait for it to unwind."""
        task = self._task
        self._task = None
        if self._finalizer is not None:
            # Avoid the finalize callback firing later on an already-done task.
            self._finalizer.detach()
            self._finalizer = None
        if task is not None and not task.done():
            task.cancel()
            # Suppress both CancelledError (the expected outcome of the
            # cancel) and any unrelated exception the polling task may
            # surface during teardown — we're shutting down, not
            # recovering.
            import contextlib

            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def __aenter__(self) -> RevocationAwareCache:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- internal -------------------------------------------------------

    async def _poll_loop(self) -> None:
        try:
            async for entry in subscribe_revocations(
                self._origin,
                since=self._since,
                poll_interval=self._poll_interval,
                client=self._client,
            ):
                evicted = self._store.pop(entry.charter_id, None)
                if evicted is not None:
                    _log.info(
                        "revocation cache: evicted",
                        extra={
                            "charter_id": entry.charter_id,
                            "principal_id": entry.principal_id,
                            "agent_id": entry.agent_id,
                            "outcome": "evicted",
                        },
                    )
                self._since = max(self._since, entry.seq)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # A polling task must never crash silently — log and exit.
            _log.error(
                "revocation cache: poll loop crashed",
                extra={"error": str(e), "outcome": "poll_crash"},
            )


def _cancel_task_if_running(task: asyncio.Task[None]) -> None:
    """weakref.finalize callback — cancel a dangling polling task."""
    if not task.done():
        task.cancel()
