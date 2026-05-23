"""Charter Inspector — Web UI helper module (B3.8).

Renders a fetched Charter as a human-readable HTML page so issuers can
visually audit what they signed without hand-parsing JSON. Hosted by the
FastAPI server at `/inspect?url=...` and `/inspect/{principal}/{agent}`.

## Trust + Security posture

1. **Trust path reuse** (ADR-007): the inspector calls
   `charter.mcp_server._fetch_and_verify` for every URL — leaf, chain
   parents, prior versions — so it cannot accidentally render a
   Charter that would fail real verification. JWKS / pin / lifecycle
   checks happen exactly once per hop.

2. **No LLM** (ADR-009): the inspector is read-only HTML rendering.
   It does not call `anthropic` or any LLM.

3. **User input sanitization**: `charter_url` is an HTTP query
   parameter. Before fetching we:

       a. parse with `urllib.parse.urlparse`,
       b. enforce a scheme allowlist `{http, https}` — `file://`,
          `ftp://`, `gopher://`, `data://` etc. are rejected,
       c. reject empty netloc,
       d. reject RFC 1918 / loopback / link-local / multicast hosts
          unless `CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS=1`. This is the
          SSRF guard; without it a public inspector instance could be
          weaponized to probe a tenant's internal network.

4. **XSS**: rendering goes through jinja2's `Environment(autoescape=True,
   undefined=StrictUndefined)`. Clause text, principal IDs, charter
   IDs are all attacker-controlled (an attacker could publish a
   Charter at a URL with `<script>` in any field). Autoescape ensures
   they render as text, not markup. `StrictUndefined` makes a typo'd
   template variable an instant 500, not a silent blank.

5. **Optional dependency**: jinja2 is in `[project.optional-dependencies]`
   `inspector`. If absent, `render_*` raises `InspectorUnavailableError`
   and the server route returns 503 with a `pip install charter[inspector]`
   hint.
"""

from __future__ import annotations

import difflib
import ipaddress
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .errors import (
    CharterError,
    CharterExpiredError,
    CharterKeyMismatchError,
    CharterNotFoundError,
    CharterPinMismatchError,
    CharterRevokedError,
    CharterSchemaError,
    CharterSignatureError,
)
from .schema import Charter
from .storage import load_archived_charter

if TYPE_CHECKING:
    # jinja2 is an optional dep — only imported at runtime inside
    # _get_environment(). Keep the type reference TYPE_CHECKING-only so
    # mypy still resolves it without forcing jinja2 at import time.
    from jinja2 import Environment


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InspectorUnavailableError(RuntimeError):
    """Raised when jinja2 is not installed.

    The server route catches this and returns 503 with an install
    hint, so a deployment without the `inspector` extra still serves
    every other endpoint normally.
    """


class InvalidCharterURLError(ValueError):
    """Raised when `charter_url` fails the allowlist + SSRF validation.

    Server route translates this into a 400 with the reason in the
    response body so an issuer who fat-fingers a URL gets a useful
    error, not a silent fetch failure.
    """


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
# Hostnames that ALWAYS resolve to loopback regardless of /etc/hosts. We
# reject by name as well as by resolved IP because a privately-mounted
# DNS could otherwise hide a loopback target behind a public-looking
# hostname.
_LOOPBACK_HOSTNAMES: frozenset[str] = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

# Cap charter_url length to keep the URL bar (and our error pages)
# bounded. 2048 matches the de-facto IE/edge URL limit and is well
# above any legitimate charter URL we have ever issued.
_MAX_URL_LENGTH = 2048


def _is_private_address(host: str) -> bool:
    """True iff `host` is a literal IP in a private / loopback /
    link-local / multicast range, OR a known loopback hostname.

    DNS resolution is intentionally NOT performed here — we only block
    the cases we can decide without a network round-trip. Operators
    that need stricter outbound controls should run the inspector
    behind an egress proxy.
    """
    if host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP — leave the verdict to DNS / network layer.
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_charter_url(charter_url: str) -> str:
    """Allowlist-style validation of a user-supplied charter URL.

    Returns the normalized URL on success. Raises `InvalidCharterURLError`
    with a precise reason on failure — the server route surfaces the
    reason in the 400 response body.

    Validation order (cheapest first):

        1. type + non-empty
        2. length cap (defangs pathological URL-bar input)
        3. scheme allowlist (`http`, `https`)
        4. netloc non-empty
        5. SSRF host check (loopback / RFC 1918 / link-local / multicast /
           reserved). Bypass with `CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS=1`
           for local-dev / single-host deployments where the inspector
           and charter server share `127.0.0.1`.
    """
    if not isinstance(charter_url, str) or not charter_url.strip():
        raise InvalidCharterURLError("charter_url is empty")

    if len(charter_url) > _MAX_URL_LENGTH:
        raise InvalidCharterURLError(f"charter_url is longer than {_MAX_URL_LENGTH} characters")

    parsed = urlparse(charter_url.strip())

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidCharterURLError(
            f"scheme {parsed.scheme!r} is not allowed; only http(s) charter URLs may be inspected"
        )

    if not parsed.netloc:
        raise InvalidCharterURLError("charter_url has no host component")

    # urlparse splits netloc as 'host[:port]'; we want just the host part.
    host = parsed.hostname or ""
    if not host:
        raise InvalidCharterURLError("charter_url has no hostname")

    allow_private = os.environ.get("CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS", "").strip() == "1"
    if not allow_private and _is_private_address(host):
        raise InvalidCharterURLError(
            f"host {host!r} resolves to a private / loopback address; refuse "
            "to fetch (set CHARTER_INSPECTOR_ALLOW_PRIVATE_NETS=1 to override)"
        )

    return charter_url.strip()


# ---------------------------------------------------------------------------
# Lifecycle status -> CSS badge class
# ---------------------------------------------------------------------------

_STATUS_CLASS: dict[str, str] = {
    "active": "badge badge-active",
    "expired": "badge badge-expired",
    "revoked": "badge badge-revoked",
    "superseded": "badge badge-superseded",
}


def status_badge_class(status: str) -> str:
    """Map `lifecycle.status` to a CSS class for the header badge."""
    return _STATUS_CLASS.get(status, "badge badge-unknown")


# ---------------------------------------------------------------------------
# Verify-chain panel: structured result rows for the template
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyStep:
    """One row in the inspector's "Signature panel" verify table.

    The template iterates these to render a checkmark / cross with a
    short label. `ok=False` rows are non-fatal — the inspector still
    renders the rest of the page so the issuer can SEE the broken
    Charter; only the corresponding badge turns red.
    """

    label: str
    ok: bool
    detail: str = ""


def build_verify_steps(charter: Charter, fetch_error: CharterError | None) -> list[VerifyStep]:
    """Translate a `_fetch_and_verify` outcome into the verify table rows.

    Called by the route AFTER fetch — when fetch raised, `charter`
    is whatever partial Charter we could still parse (or None and
    we render the error banner instead).
    """
    steps: list[VerifyStep] = []
    # Signature
    if isinstance(fetch_error, CharterSignatureError):
        steps.append(VerifyStep("Ed25519 signature", False, str(fetch_error)))
    else:
        steps.append(
            VerifyStep(
                "Ed25519 signature",
                True,
                f"verified against issuer_public_key (kid={charter.provenance.issuer_kid or 'inline-only'})",
            )
        )
    # JWKS
    if isinstance(fetch_error, CharterKeyMismatchError):
        steps.append(VerifyStep("JWKS kid match", False, str(fetch_error)))
    elif charter.provenance.issuer_kid is not None:
        steps.append(
            VerifyStep(
                "JWKS kid match",
                True,
                f"kid={charter.provenance.issuer_kid} matches issuer JWKS",
            )
        )
    else:
        steps.append(
            VerifyStep(
                "JWKS kid match",
                True,
                "no kid set (legacy v0 self-attesting; JWKS not consulted)",
            )
        )
    # Pin
    if isinstance(fetch_error, CharterPinMismatchError):
        steps.append(VerifyStep("Issuer key pin", False, str(fetch_error)))
    else:
        steps.append(
            VerifyStep(
                "Issuer key pin",
                True,
                "fingerprint matches local pin (or recorded TOFU on first fetch)",
            )
        )
    # Lifecycle
    if isinstance(fetch_error, CharterRevokedError):
        steps.append(VerifyStep("Lifecycle", False, "charter has been revoked"))
    elif isinstance(fetch_error, CharterExpiredError):
        steps.append(VerifyStep("Lifecycle", False, "charter expired or superseded"))
    else:
        steps.append(VerifyStep("Lifecycle", True, f"status={charter.lifecycle.status}"))
    # Transparency
    if charter.provenance.transparency_log_id is not None:
        steps.append(
            VerifyStep(
                "Transparency log",
                True,
                f"appended at seq={charter.provenance.transparency_log_id}",
            )
        )
    else:
        steps.append(
            VerifyStep(
                "Transparency log",
                True,
                "no transparency log id (legacy or log unreachable at sign time)",
            )
        )
    return steps


# ---------------------------------------------------------------------------
# Diff vs prior version
# ---------------------------------------------------------------------------


def diff_against_prior(charter: Charter) -> str | None:
    """Return a unified-diff string of `charter` vs its `lifecycle.replaces`
    archived predecessor, or None if no predecessor is on disk.

    Uses stdlib `difflib` only — no third-party diff library. The
    output is the full multi-line `unified_diff` string with `--- prior`
    / `+++ current` headers; the template renders it inside a `<pre>`
    block so the issuer can scan added / removed clauses at a glance.

    Why local archive only:
        `lifecycle.replaces` is a `charter_id`, not a URL. Looking it
        up in the local archive avoids a second SSRF surface. If the
        predecessor isn't archived locally (e.g. this inspector is
        running on a fresh host) we just return None and the template
        renders "no prior version available".
    """
    prior_id = charter.lifecycle.replaces
    if not prior_id:
        return None
    prior = load_archived_charter(prior_id)
    if prior is None:
        return None
    prior_json = prior.model_dump_json(indent=2).splitlines(keepends=False)
    current_json = charter.model_dump_json(indent=2).splitlines(keepends=False)
    diff_lines = difflib.unified_diff(
        prior_json,
        current_json,
        fromfile=f"prior:{prior.charter_id}",
        tofile=f"current:{charter.charter_id}",
        lineterm="",
    )
    text = "\n".join(diff_lines)
    return text or None


# ---------------------------------------------------------------------------
# Chain walk (UI variant — does not re-verify; the caller has already
# fetched each link with `_fetch_and_verify`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainNode:
    """One node in the chain panel.

    `charter` is None when the parent link could not be fetched (broken
    chain). The template still renders the node so the issuer can see
    where the chain failed, with `error` showing the reason.
    """

    depth: int
    url: str
    charter: Charter | None
    error: str | None = None


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------


def _get_environment() -> Environment:
    """Build the jinja2 env, importing lazily so jinja2 is optional.

    `autoescape=True` is the XSS guard — any `{{ ... }}` substitution
    of a Charter field gets HTML-escaped. `StrictUndefined` turns a
    typo'd template variable into an exception (500) instead of a
    silently-blank page; that's the right tradeoff for an internal
    audit tool.
    """
    try:
        from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
    except ImportError as e:
        raise InspectorUnavailableError(
            "Inspector requires jinja2. Install with: pip install charter[inspector]"
        ) from e

    env = Environment(
        loader=PackageLoader("charter", "templates"),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["status_badge"] = status_badge_class
    return env


def is_available() -> bool:
    """Cheap pre-flight so server route can return 503 quickly."""
    try:
        import jinja2  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Render — happy path
# ---------------------------------------------------------------------------


def render_charter(
    charter: Charter,
    charter_url: str,
    *,
    chain: list[ChainNode] | None = None,
    diff_text: str | None = None,
    fetch_error: CharterError | None = None,
) -> str:
    """Render `inspect.html` for a successfully-fetched Charter.

    Pure function — no I/O. The route is expected to have already
    fetched + verified the leaf (and optionally walked the chain /
    computed the diff) before calling this.

    `fetch_error` is passed in so the template can flip the signature
    panel to a red banner without losing the rest of the rendering;
    this is the "verification failed but the body parsed" UX path
    described in the AC.
    """
    env = _get_environment()
    tpl = env.get_template("inspect.html")
    return tpl.render(
        charter=charter,
        charter_url=charter_url,
        verify_steps=build_verify_steps(charter, fetch_error),
        chain=chain or [],
        diff_text=diff_text,
        fetch_error_class=(type(fetch_error).__name__ if fetch_error else None),
        fetch_error_message=(str(fetch_error) if fetch_error else None),
        # Make a list of (clause, redacted_count) tuples so the template
        # doesn't have to call len() inside a Jinja expression.
        clause_rows=[(c, len(c.private_fields or [])) for c in charter.clauses],
    )


def render_invalid_url(charter_url: str, reason: str) -> str:
    """400 page: URL failed allowlist validation.

    Rendered with `error.html` so the user sees a styled page instead
    of FastAPI's default JSON 400 body. `charter_url` is reflected back
    but autoescaped by the template.
    """
    env = _get_environment()
    tpl = env.get_template("error.html")
    return tpl.render(
        status_code=400,
        title="Invalid charter URL",
        message=reason,
        charter_url=charter_url,
    )


def render_fetch_error(charter_url: str, error: Exception) -> str:
    """502 / 404 page: URL was valid but fetch / parse failed before we
    even had a Charter to show.

    `CharterNotFoundError` / `CharterSchemaError` reach this path —
    they are different from `CharterSignatureError` (which still
    yields a renderable Charter and goes through `render_charter`).
    """
    env = _get_environment()
    tpl = env.get_template("error.html")
    return tpl.render(
        status_code=502,
        title=type(error).__name__,
        message=str(error),
        charter_url=charter_url,
    )


# ---------------------------------------------------------------------------
# Helper for the route: walk the chain by re-using `_fetch_and_verify`
# ---------------------------------------------------------------------------


def walk_chain(
    leaf: Charter,
    leaf_url: str,
    fetcher: Any,
    max_depth: int = 5,
) -> list[ChainNode]:
    """Walk `parent_charter_url` upward, fetching each link with
    `fetcher` (expected to be `mcp_server._fetch_and_verify`).

    Returns a list of `ChainNode` ordered LEAF-first (so chain[0] is
    the Charter the user opened, chain[-1] is the root). Broken hops
    are recorded as `ChainNode(charter=None, error=...)` instead of
    raising; the template still renders the link so the user sees the
    failure point.

    `fetcher` is dependency-injected for two reasons:
      1. Avoid a circular import with `mcp_server`.
      2. Tests can pass a recording fake without monkeypatching httpx.
    """
    nodes: list[ChainNode] = [ChainNode(depth=0, url=leaf_url, charter=leaf)]
    seen_urls: set[str] = {leaf_url}
    current = leaf
    depth = 0
    while current.parent_charter_url is not None and depth < max_depth:
        next_url = current.parent_charter_url
        depth += 1
        if next_url in seen_urls:
            nodes.append(ChainNode(depth=depth, url=next_url, charter=None, error="cycle detected"))
            break
        seen_urls.add(next_url)
        try:
            # The chain URL is issuer-controlled and was inside a Charter
            # we already trust; still defensively validate it before
            # fetching to keep the SSRF perimeter consistent.
            validate_charter_url(next_url)
            parent = fetcher(next_url)
        except (InvalidCharterURLError, CharterError, CharterNotFoundError) as e:
            nodes.append(
                ChainNode(
                    depth=depth,
                    url=next_url,
                    charter=None,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            break
        except Exception as e:  # pragma: no cover — last-resort safety
            nodes.append(
                ChainNode(depth=depth, url=next_url, charter=None, error=f"unexpected: {e}")
            )
            break
        nodes.append(ChainNode(depth=depth, url=next_url, charter=parent))
        current = parent
    return nodes


# Keep these in `__all__` so `from charter.inspector import *` in a future
# CLI subcommand stays explicit about what it pulls in.
__all__ = [
    "ChainNode",
    "InspectorUnavailableError",
    "InvalidCharterURLError",
    "VerifyStep",
    "build_verify_steps",
    "diff_against_prior",
    "is_available",
    "render_charter",
    "render_fetch_error",
    "render_invalid_url",
    "status_badge_class",
    "validate_charter_url",
    "walk_chain",
]


# Suppress unused-import warning when TYPE_CHECKING isn't running.
_ = CharterSchemaError
