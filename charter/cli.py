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

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
from dotenv import load_dotenv

from .constants import DEFAULT_URL_BASE, DEFAULT_VALID_DAYS
from .projection import load_profile, project
from .schema import Lifecycle
from .signing import sign_charter
from .storage import archive_charter, ensure_issuer_key, load_charter, save_charter

load_dotenv()


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

    click.echo("")
    click.echo(click.style("[OK] Charter renewed", fg="green", bold=True))
    click.echo(f"  old charter_id: {old.charter_id}  (status=superseded)")
    click.echo(f"  new charter_id: {new.charter_id}")
    click.echo(f"  valid_until:    {new.lifecycle.valid_until.isoformat()}")
    click.echo(f"  live file:      {saved}")
    click.echo(f"  archived file:  {archived}")
    click.echo("")


def main() -> None:
    """Console-script entry point: `charter`."""
    cli()


if __name__ == "__main__":
    main()
