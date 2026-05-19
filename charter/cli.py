"""Charter CLI.

Subcommands:

    charter issue   <profile.yaml>
        Load profile -> project clauses via LLM -> sign -> save to disk.
        Prints the resulting charter_url.

    charter inspect <principal_id> <agent_id>
        Pretty-print a previously issued Charter from local storage.

    charter revoke  <principal_id> <agent_id>
        Mark a stored Charter as revoked. Re-signs so the revocation
        itself is verifiable; the next fetch_charter call will raise
        CharterRevokedError.

    charter renew   <principal_id> <agent_id>
        Issue a fresh Charter for the same binding with identical
        clauses + summary, a new validity window, and the old Charter
        marked as superseded. No LLM call (vs. `issue`, which re-runs
        projection).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import click
from dotenv import load_dotenv

if TYPE_CHECKING:
    from .transparency import ChainVerification, TransparencyEntry

from ._logging import get_logger
from .constants import DEFAULT_URL_BASE, DEFAULT_VALID_DAYS
from .projection import load_profile, project
from .schema import Lifecycle
from .signing import sign_charter
from .storage import archive_charter, ensure_issuer_key, load_charter, save_charter

load_dotenv()

_log_issue = get_logger("charter.cli.issue")
_log_revoke = get_logger("charter.cli.revoke")
_log_renew = get_logger("charter.cli.renew")
_log_inspect = get_logger("charter.cli.inspect")


@click.group()
def cli() -> None:
    """Charter — Authority layer between Agent Card and AP2 Mandate."""


@cli.command()
@click.argument("profile_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def issue(profile_path: Path) -> None:
    """Issue a Charter from a profile.yaml. One-shot projection + sign + save.

    Example:
        charter issue profiles/alice.yaml
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo(
            "ERROR: ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill in your key.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"[1/4] Loaded profile: {profile_path}")
    profile, raw = load_profile(profile_path)

    click.echo(
        f"[2/4] Projecting via Claude ({os.environ.get('CHARTER_MODEL', 'claude-sonnet-4-6')})..."
    )
    private_key = ensure_issuer_key(profile.principal.id)
    charter = project(profile, raw, private_key)

    click.echo("[3/4] Signing with issuer key (ed25519)...")
    sign_charter(charter, private_key)

    click.echo("[4/4] Saving locally...")
    saved = save_charter(charter)

    base = os.environ.get("CHARTER_URL_BASE", DEFAULT_URL_BASE).rstrip("/")
    charter_url = f"{base}/{profile.principal.id}/{profile.agent.id}"

    _log_issue.info(
        "charter issued",
        extra={
            "charter_id": charter.charter_id,
            "principal_id": profile.principal.id,
            "agent_id": profile.agent.id,
            "outcome": "ok",
        },
    )

    click.echo("")
    click.echo(click.style("[OK] Charter active", fg="green", bold=True))
    click.echo(f"  charter_id:  {charter.charter_id}")
    click.echo(f"  binding:     {profile.principal.id} x {profile.agent.id}")
    click.echo(f"  valid_until: {charter.lifecycle.valid_until.isoformat()}")
    click.echo(f"  file:        {saved}")
    click.echo(f"  url:         {charter_url}")
    click.echo("")
    click.echo("  (Make sure `charter-server` is running for the URL to resolve.)")


@cli.command()
@click.argument("principal_id")
@click.argument("agent_id")
def inspect(principal_id: str, agent_id: str) -> None:
    """Pretty-print a stored Charter.

    Example:
        charter inspect alice@acme.com research_agent_v1
    """
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        click.echo(
            f"No Charter found for {principal_id} × {agent_id}. Did you run `charter issue` first?",
            err=True,
        )
        sys.exit(1)

    click.echo("")
    click.echo(click.style(f"Charter: {charter.charter_id}", bold=True))
    click.echo(f"  Principal:  {charter.principal.id}  ({charter.principal.role_summary})")
    click.echo(f"  Agent:      {charter.binding.agent_id}")
    click.echo(f"  Issuer:     {charter.issuer.id}")
    click.echo(
        f"  Valid:      {charter.lifecycle.issued_at.date()}  ->  {charter.lifecycle.valid_until.date()}  ({charter.lifecycle.status})"
    )
    click.echo("")
    click.echo(click.style("Summary:", bold=True))
    click.echo(f"  {charter.summary.plain_language}")
    click.echo("")
    click.echo(click.style(f"Clauses ({len(charter.clauses)}):", bold=True))
    for c in charter.clauses:
        color = {
            "scope": "green",
            "out_of_scope": "red",
            "approval_required": "yellow",
            "operational_limit": "yellow",
            "style": "cyan",
            "data_handling": "yellow",
        }.get(c.type, "white")
        click.echo(
            f"  {click.style(c.id, bold=True)}  {click.style(c.type.ljust(18), fg=color)}  {c.text}"
        )
    click.echo("")


@cli.command()
@click.argument("principal_id")
@click.argument("agent_id")
def revoke(principal_id: str, agent_id: str) -> None:
    """Revoke a stored Charter.

    Flips `lifecycle.status` to `"revoked"`, sets `revoked_at` to now,
    and re-signs so the revocation is itself cryptographically attested.
    After this, `fetch_charter` will raise `CharterRevokedError`.

    Example:
        charter revoke alice@acme.com research_agent_v1
    """
    charter = load_charter(principal_id, agent_id)
    if charter is None:
        click.echo(
            f"No Charter found for {principal_id} x {agent_id}.",
            err=True,
        )
        sys.exit(1)

    if charter.lifecycle.status == "revoked":
        click.echo(
            f"Charter is already revoked (revoked_at={charter.lifecycle.revoked_at}).",
            err=True,
        )
        sys.exit(1)

    now = datetime.now(UTC).replace(microsecond=0)
    charter.lifecycle.status = "revoked"
    charter.lifecycle.revoked_at = now
    # Clear the signature so canonical_bytes recomputes correctly during re-sign.
    charter.provenance.issuer_signature = ""

    private_key = ensure_issuer_key(principal_id)
    sign_charter(charter, private_key)

    saved = save_charter(charter)

    _log_revoke.info(
        "charter revoked",
        extra={
            "charter_id": charter.charter_id,
            "principal_id": principal_id,
            "agent_id": agent_id,
            "outcome": "ok",
        },
    )

    click.echo("")
    click.echo(click.style("[OK] Charter revoked", fg="red", bold=True))
    click.echo(f"  charter_id:  {charter.charter_id}")
    click.echo(f"  binding:     {principal_id} x {agent_id}")
    click.echo(f"  revoked_at:  {now.isoformat()}")
    click.echo(f"  file:        {saved}")
    click.echo("")
    click.echo("  Calling agents will see CharterRevokedError on next fetch.")


@cli.command()
@click.argument("principal_id")
@click.argument("agent_id")
@click.option(
    "--valid-days",
    type=int,
    default=None,
    help=f"Override validity window in days (default: {DEFAULT_VALID_DAYS}).",
)
def renew(principal_id: str, agent_id: str, valid_days: int | None) -> None:
    """Renew a stored Charter without re-running projection.

    Builds a fresh Charter with the same clauses + summary as the
    current one, a new `charter_id`, a new validity window, and
    `replaces` pointing at the old Charter. The old Charter is marked
    `status="superseded"` with `replaced_by` pointing at the new one.
    Both are re-signed and saved.

    Example:
        charter renew alice@acme.com research_agent_v1
        charter renew alice@acme.com research_agent_v1 --valid-days 60
    """
    old = load_charter(principal_id, agent_id)
    if old is None:
        click.echo(
            f"No Charter found for {principal_id} x {agent_id}.",
            err=True,
        )
        sys.exit(1)

    if old.lifecycle.status not in ("active", "expired"):
        click.echo(
            f"Cannot renew a Charter with status={old.lifecycle.status!r}. "
            "Only `active` or `expired` Charters are renewable.",
            err=True,
        )
        sys.exit(1)

    now = datetime.now(UTC).replace(microsecond=0)
    window_days = valid_days if valid_days is not None else DEFAULT_VALID_DAYS

    new_charter_id = f"charter:{principal_id}:{agent_id}:{now.date().isoformat()}"
    if new_charter_id == old.charter_id:
        # Same day rename — append a marker to keep the ID unique.
        new_charter_id += f":renewed-{now.strftime('%H%M%S')}"

    # Build the new Charter from a deep copy of the old, then mutate the
    # bits that change. Pydantic's model_copy(deep=True) preserves clauses,
    # summary, and provenance.source_commitments.
    new = old.model_copy(deep=True)
    new.charter_id = new_charter_id
    new.lifecycle = Lifecycle(
        issued_at=now,
        valid_until=now + timedelta(days=window_days),
        status="active",
        replaces=old.charter_id,
    )
    new.provenance.generated_at = now
    new.provenance.issuer_signature = ""  # cleared so re-sign starts fresh

    # Mark old as superseded.
    old.lifecycle.status = "superseded"
    old.lifecycle.replaced_by = new.charter_id
    old.provenance.issuer_signature = ""

    private_key = ensure_issuer_key(principal_id)
    sign_charter(new, private_key)
    sign_charter(old, private_key)

    # The canonical binding path (data/charters/<p>__<a>.json) holds only
    # the live Charter, so the superseded predecessor goes to the archive
    # subdirectory keyed by charter_id. The new Charter then overwrites
    # the binding path.
    archived = archive_charter(old)
    saved = save_charter(new)

    _log_renew.info(
        "charter renewed",
        extra={
            "principal_id": principal_id,
            "agent_id": agent_id,
            "old_charter_id": old.charter_id,
            "new_charter_id": new.charter_id,
            "outcome": "ok",
        },
    )

    click.echo("")
    click.echo(click.style("[OK] Charter renewed", fg="green", bold=True))
    click.echo(f"  old charter_id: {old.charter_id}  (status=superseded)")
    click.echo(f"  new charter_id: {new.charter_id}")
    click.echo(f"  valid_until:    {new.lifecycle.valid_until.isoformat()}")
    click.echo(f"  live file:      {saved}")
    click.echo(f"  archived file:  {archived}")
    click.echo("")


@cli.group()
def audit() -> None:
    """Walk and inspect the v0.8 transparency log.

    Two subcommands:

        charter audit verify [--remote URL] [--since SEQ]
            Walks the transparency log (local data/transparency.log by
            default, or a remote `/transparency/log` endpoint) and
            verifies the SHA-256 chain. Exit code 0 on success, 1 on
            chain break, 2 on network or parse failure.

        charter audit show <charter_id>
            Pretty-prints the transparency entry for a Charter, plus a
            list of every entry that shares the same issuer or binding.
    """


@audit.command("verify")
@click.option(
    "--remote",
    type=str,
    default=None,
    metavar="ORIGIN",
    help=(
        "Remote issuer origin (e.g. https://charter.example.com). "
        "Fetches /transparency/log from that origin instead of reading "
        "the local data/transparency.log."
    ),
)
@click.option(
    "--since",
    type=int,
    default=0,
    metavar="SEQ",
    help="Skip entries with seq <= SEQ. Useful for resuming an audit.",
)
def audit_verify(remote: str | None, since: int) -> None:
    """Verify the SHA-256 chain of the transparency log."""
    from . import transparency

    if remote is not None:
        try:
            entries = _fetch_remote_log(remote, since=since)
        except _RemoteFetchError as e:
            click.echo(click.style(f"[ERROR] {e}", fg="red", bold=True), err=True)
            sys.exit(2)
        result = _verify_entries(entries, since=since)
    else:
        # Local mode delegates to the in-process verifier when --since is
        # 0 (full chain), and slices the local log when --since > 0 so we
        # match what a remote /transparency/log?since=N call would see.
        if since == 0:
            result = transparency.verify_chain()
        else:
            sliced = [e for e in transparency.read_log() if e.seq > since]
            result = _verify_entries(sliced, since=since)

    if result.ok:
        scope = "remote" if remote is not None else "local"
        click.echo(click.style("[OK] Transparency log verified", fg="green", bold=True))
        click.echo(f"  source:        {remote if remote else 'data/transparency.log'} ({scope})")
        click.echo(f"  entries:       {result.entries}")
        if result.entries > 0:
            click.echo(f"  range:         seq {since + 1} -> seq {since + result.entries}")
            click.echo(f"  head_hash:     {result.head_hash}")
        else:
            click.echo("  (log is empty)")
        sys.exit(0)

    click.echo(
        click.style(f"[ERROR] Chain broken at seq {result.broken_at_seq}", fg="red", bold=True),
        err=True,
    )
    click.echo(f"  reason: {result.reason}", err=True)
    sys.exit(1)


@audit.command("show")
@click.argument("charter_id")
def audit_show(charter_id: str) -> None:
    """Pretty-print a Charter's transparency entry + related entries.

    "Related" means anything that shares the issuer (principal_id) or
    the full binding. Useful for the question "show me every Charter
    this issuer has ever signed."
    """
    from . import transparency

    target = transparency.get_entry(charter_id)
    if target is None:
        click.echo(f"No transparency entry found for {charter_id}.", err=True)
        sys.exit(1)

    click.echo("")
    click.echo(click.style(f"Charter: {target.charter_id}", bold=True))
    click.echo(f"  seq:              {target.seq}")
    click.echo(f"  principal_id:     {target.binding['principal_id']}")
    click.echo(f"  agent_id:         {target.binding['agent_id']}")
    click.echo(f"  issuer_kid:       {target.issuer_kid}")
    click.echo(f"  appended_at:      {target.appended_at.isoformat()}")
    click.echo(f"  prev_hash:        {target.prev_hash}")
    click.echo(f"  entry_hash:       {target.entry_hash}")

    # Related: same principal_id (treat principal as the issuer for the
    # self-attesting Charters we ship by default).
    related = [
        e
        for e in transparency.read_log()
        if e.charter_id != target.charter_id
        and e.binding["principal_id"] == target.binding["principal_id"]
    ]
    if related:
        click.echo("")
        click.echo(
            click.style(
                f"Other entries from {target.binding['principal_id']} ({len(related)}):",
                bold=True,
            )
        )
        for e in related:
            same_binding = e.binding["agent_id"] == target.binding["agent_id"]
            marker = "*" if same_binding else " "
            click.echo(
                f"  {marker} seq {e.seq:>4}  {e.charter_id}  "
                f"({e.binding['agent_id']}, {e.appended_at.date()})"
            )
        click.echo("")
        click.echo("  ('*' marks entries that share this exact binding.)")
    click.echo("")


# ----- audit helpers ------------------------------------------------------


class _RemoteFetchError(Exception):
    """Raised by `_fetch_remote_log` for any HTTP / parse failure. CLI
    converts to exit code 2."""


def _fetch_remote_log(origin: str, *, since: int) -> list[TransparencyEntry]:
    """Fetch `/transparency/log[?since=N]` from a remote issuer origin and
    parse the NDJSON body into TransparencyEntry objects."""
    import httpx

    from .transparency import TransparencyEntry

    url = origin.rstrip("/") + "/transparency/log"
    params = {"since": since} if since > 0 else None
    try:
        resp = httpx.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise _RemoteFetchError(f"GET {url} -> HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        raise _RemoteFetchError(f"GET {url} failed: {e}") from e

    entries: list[TransparencyEntry] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            entries.append(TransparencyEntry.from_dict(raw))
        except (ValueError, KeyError, TypeError) as e:
            raise _RemoteFetchError(f"unparseable log line: {e}") from e
    return entries


def _verify_entries(entries: list[TransparencyEntry], *, since: int) -> ChainVerification:
    """Run the same SHA-256 chain check as transparency.verify_chain but
    on an arbitrary slice (used for `--since` and `--remote`)."""
    from .transparency import (
        GENESIS_PREV_HASH,
        ChainVerification,
        _hash_entry_fields,
    )

    if not entries:
        return ChainVerification(
            ok=True,
            entries=0,
            head_hash=GENESIS_PREV_HASH,
            broken_at_seq=None,
            reason=None,
        )

    # For a since-sliced verification we still need a known-good anchor
    # for the first entry's prev_hash. The caller passing since=N means
    # "verify entries with seq > N"; we trust that the entry at seq=N+1
    # carries a prev_hash that points at the unseen seq=N. We can verify
    # the chain INTERNALLY but cannot prove the first entry's prev_hash
    # without seq=N. That's fine for the `verify` UX — the audit log is
    # public, so a follow-up `--since 0` would catch any tampering of
    # the unseen prefix.
    expected_prev = entries[0].prev_hash if since > 0 else GENESIS_PREV_HASH

    for entry in entries:
        if entry.prev_hash != expected_prev:
            return ChainVerification(
                ok=False,
                entries=len(entries),
                head_hash=entries[-1].entry_hash,
                broken_at_seq=entry.seq,
                reason=(
                    f"prev_hash mismatch at seq={entry.seq}: "
                    f"expected {expected_prev}, found {entry.prev_hash}"
                ),
            )
        recomputed = _hash_entry_fields(
            {k: v for k, v in entry.to_dict().items() if k != "entry_hash"}
        )
        if recomputed != entry.entry_hash:
            return ChainVerification(
                ok=False,
                entries=len(entries),
                head_hash=entries[-1].entry_hash,
                broken_at_seq=entry.seq,
                reason=(
                    f"entry_hash mismatch at seq={entry.seq}: "
                    f"recomputed {recomputed}, found {entry.entry_hash}"
                ),
            )
        expected_prev = entry.entry_hash

    return ChainVerification(
        ok=True,
        entries=len(entries),
        head_hash=entries[-1].entry_hash,
        broken_at_seq=None,
        reason=None,
    )


@cli.group()
def pins() -> None:
    """Inspect or reset issuer-key fingerprint pins (v0.8 trust model).

    Pins live in `data/pins.json` (override with `CHARTER_PIN_FILE`).
    Each pin records the SHA-256 fingerprint of an issuer's signing key
    the first time we see it, so a later swap to a different key fires
    `CharterPinMismatchError` at fetch time. Run `charter pins reset`
    after a *legitimate* key rotation to authorize the new fingerprint.
    """


@pins.command("list")
def pins_list() -> None:
    """Pretty-print the current pin table."""
    from .pins import list_pins

    table = list_pins()
    if not table:
        click.echo("No pins recorded yet.")
        return

    click.echo("")
    click.echo(click.style(f"Pinned issuers ({len(table)}):", bold=True))
    for principal_id, pin in sorted(table.items()):
        click.echo(f"  {click.style(principal_id, bold=True)}")
        click.echo(f"    fingerprint:   {pin.fingerprint}")
        click.echo(f"    first_seen:    {pin.first_seen.isoformat()}")
        click.echo(f"    last_verified: {pin.last_verified.isoformat()}")
    click.echo("")


@pins.command("reset")
@click.argument("principal_id")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def pins_reset(principal_id: str, yes: bool) -> None:
    """Drop the pin for PRINCIPAL_ID.

    Use this after a legitimate key rotation. The next fetch will
    establish a fresh pin. Prints the current fingerprint and asks for
    confirmation unless `--yes` is given.

    Example:
        charter pins reset alice@acme.com
    """
    from .pins import get_pin, reset_pin

    pin = get_pin(principal_id)
    if pin is None:
        click.echo(f"No pin recorded for {principal_id}.", err=True)
        sys.exit(1)

    click.echo("")
    click.echo(f"  principal:     {principal_id}")
    click.echo(f"  fingerprint:   {pin.fingerprint}")
    click.echo(f"  first_seen:    {pin.first_seen.isoformat()}")
    click.echo(f"  last_verified: {pin.last_verified.isoformat()}")
    click.echo("")

    if not yes:
        click.confirm(
            f"Drop the pin for {principal_id}? Only do this after a LEGITIMATE key rotation.",
            abort=True,
        )

    reset_pin(principal_id)
    click.echo(click.style(f"[OK] Pin dropped for {principal_id}.", fg="yellow", bold=True))
    click.echo("  The next fetch will establish a fresh pin.")


def main() -> None:
    """Console-script entry point: `charter`."""
    cli()


if __name__ == "__main__":
    main()
