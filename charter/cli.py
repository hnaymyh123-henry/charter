"""Charter CLI — `charter issue` and `charter inspect`.

    charter issue <profile.yaml>
        Load profile -> project clauses via LLM -> sign -> save to disk.
        Prints the resulting charter_url.

    charter inspect <principal_id> <agent_id>
        Pretty-print a previously issued Charter from local storage.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from .constants import DEFAULT_URL_BASE
from .projection import load_profile, project
from .signing import sign_charter
from .storage import ensure_issuer_key, load_charter, save_charter


load_dotenv()


@click.group()
def cli() -> None:
    """Charter — agent 经济的雇佣合同 (v0 demo)."""


@cli.command()
@click.argument("profile_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def issue(profile_path: Path) -> None:
    """Issue a Charter from a profile.yaml. One-shot projection + sign + save.

    Example:
        charter issue profiles/alice.yaml
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo(
            "ERROR: ANTHROPIC_API_KEY is not set. Copy .env.example to .env "
            "and fill in your key.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"[1/4] Loaded profile: {profile_path}")
    profile, raw = load_profile(profile_path)

    click.echo(f"[2/4] Projecting via Claude ({os.environ.get('CHARTER_MODEL', 'claude-sonnet-4-6')})...")
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
            f"No Charter found for {principal_id} × {agent_id}. "
            f"Did you run `charter issue` first?",
            err=True,
        )
        sys.exit(1)

    click.echo("")
    click.echo(click.style(f"Charter: {charter.charter_id}", bold=True))
    click.echo(f"  Principal:  {charter.principal.id}  ({charter.principal.role_summary})")
    click.echo(f"  Agent:      {charter.binding.agent_id}")
    click.echo(f"  Issuer:     {charter.issuer.id}")
    click.echo(f"  Valid:      {charter.lifecycle.issued_at.date()}  ->  {charter.lifecycle.valid_until.date()}  ({charter.lifecycle.status})")
    click.echo("")
    click.echo(click.style("Summary:", bold=True))
    click.echo(f"  {charter.summary.plain_language}")
    click.echo("")
    click.echo(click.style(f"Clauses ({len(charter.clauses)}):", bold=True))
    for c in charter.clauses:
        color = {
            "scope":             "green",
            "out_of_scope":      "red",
            "approval_required": "yellow",
            "operational_limit": "yellow",
            "style":             "cyan",
            "data_handling":     "yellow",
        }.get(c.type, "white")
        click.echo(
            f"  {click.style(c.id, bold=True)}  "
            f"{click.style(c.type.ljust(18), fg=color)}  {c.text}"
        )
    click.echo("")


def main() -> None:
    """Console-script entry point: `charter`."""
    cli()


if __name__ == "__main__":
    main()
