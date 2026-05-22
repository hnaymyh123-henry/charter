"""Minimal RFC 9421 (HTTP Message Signatures) subset used by Web Bot Auth.

This module deliberately implements only what the Charter Web Bot Auth
adapter needs:

  - Algorithm: Ed25519 only (Charter-wide constraint, ADR-002).
  - Covered components: derived (`@method`, `@path`, `@authority`),
    `content-digest` (when the request has a body), and the custom
    `charter_url` signature parameter.
  - Structured-field serialization: only `sf-string`, `sf-integer`,
    `sf-token` and the inner-list / parameter forms needed to build the
    `Signature-Input` header.

What is NOT implemented:

  - Algorithm registry (RSA, ECDSA, HMAC, ...).
  - HTTP/2-specific components (`@status`, `@request-target`, ...).
  - `Accept-Signature`, `Signature-Set`, multi-signature negotiation.
  - Trailers, request-response binding.
  - Full HTTP Structured Field Values parser (RFC 8941).

If you need any of those, replace this module with `http-message-signatures`.

Sections of RFC 9421 partially covered:

  - §2.1  HTTP fields            — only `content-digest`
  - §2.2  Derived components     — `@method`, `@path`, `@authority`
  - §2.3  Signature parameters   — `keyid`, `created`, `alg`, plus a custom
                                   `charter_url` parameter
  - §2.5  Creating the signature base (concatenation rules)
  - §3.3  HTTP Signature Algorithms — Ed25519 only
  - §4.1  The `Signature` field
  - §4.2  The `Signature-Input` field

See Issue #28 PR description for the full coverage statement.
"""

from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Constant signature label. RFC 9421 allows multiple signatures per request;
# Charter only needs one, so we hard-code the label rather than negotiate.
SIG_LABEL = "charter"

# Derived components we sign. Order is fixed — verifier must reconstruct
# the same order to recompute the same signature base.
_DERIVED_COMPONENTS = ("@method", "@path", "@authority")


# ---------------------------------------------------------------------------
# Structured Field Values — minimal sf-string / sf-token helpers
# ---------------------------------------------------------------------------


def _sf_string(value: str) -> str:
    """Serialize a Python string as an RFC 8941 sf-string.

    Backslashes and double quotes are backslash-escaped; the result is
    wrapped in double quotes.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_sf_string(raw: str) -> str:
    """Inverse of `_sf_string`. Raises ValueError on malformed input.

    Only handles the escape characters `\\` and `"`. Sufficient for the
    parameters Charter writes — we do not need full RFC 8941 coverage.
    """
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        raise ValueError(f"expected quoted sf-string, got {raw!r}")
    out: list[str] = []
    i = 1
    end = len(raw) - 1
    while i < end:
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= end:
                raise ValueError("trailing backslash in sf-string")
            out.append(raw[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Content-Digest (RFC 9530 / 9421 §2.1 example)
# ---------------------------------------------------------------------------


def compute_content_digest(body: bytes) -> str:
    """Return the `Content-Digest` header value for the given body.

    Charter only supports `sha-256` — that matches everything else in the
    project's crypto surface.
    """
    digest = hashlib.sha256(body).digest()
    return f"sha-256=:{base64.b64encode(digest).decode('ascii')}:"


def _verify_content_digest(header_value: str, body: bytes) -> bool:
    """Return True iff `header_value` is a `sha-256=:...:` digest of `body`.

    Header values from real HTTP stacks may have whitespace; we strip it.
    """
    expected = compute_content_digest(body)
    return header_value.strip() == expected


# ---------------------------------------------------------------------------
# Derived components & signature base (RFC 9421 §2.2 / §2.5)
# ---------------------------------------------------------------------------


def _derived_value(name: str, method: str, url: str) -> str:
    """Resolve a derived component to its canonical string value."""
    if name == "@method":
        # RFC 9421 §2.2.1: the method MUST be uppercased.
        return method.upper()
    parsed = urlparse(url)
    if name == "@authority":
        # RFC 9421 §2.2.3: lowercased host[:port], no userinfo.
        netloc = parsed.netloc.lower()
        # Strip any "user:pass@" prefix defensively.
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]
        return netloc
    if name == "@path":
        # RFC 9421 §2.2.5: absolute path, default "/" when empty.
        return parsed.path or "/"
    raise ValueError(f"unsupported derived component {name!r}")


def _build_signature_base(
    *,
    method: str,
    url: str,
    headers_lower: dict[str, str],
    covered_components: list[str],
    signature_params_value: str,
) -> bytes:
    """Concatenate covered components into RFC 9421 signature base bytes.

    Each line is:    "<component-id>": <value>\\n
    Final line:      "@signature-params": <signature_params_value>

    Returns the UTF-8 encoding of the full string (no trailing newline).
    """
    lines: list[str] = []
    for comp in covered_components:
        if comp.startswith("@"):
            value = _derived_value(comp, method, url)
        else:
            # Plain header component. Names lowercased per §2.1.
            raw = headers_lower.get(comp)
            if raw is None:
                raise ValueError(f"covered component {comp!r} is missing from headers")
            # Trim leading/trailing OWS as RFC 9421 specifies.
            value = raw.strip()
        lines.append(f'"{comp}": {value}')
    lines.append(f'"@signature-params": {signature_params_value}')
    return "\n".join(lines).encode("utf-8")


def _signature_params_value(
    covered_components: list[str],
    *,
    keyid: str,
    created: int,
    charter_url: str,
) -> str:
    """Build the parenthesized inner-list + parameters for Signature-Input.

    Result looks like:

        ("@method" "@path" "@authority" "content-digest");\
            keyid="..."; created=...; alg="ed25519"; charter_url="..."
    """
    inner = " ".join(_sf_string(c) for c in covered_components)
    params = ";".join(
        [
            f"keyid={_sf_string(keyid)}",
            f"created={created}",
            'alg="ed25519"',
            f"charter_url={_sf_string(charter_url)}",
        ]
    )
    return f"({inner});{params}"


# ---------------------------------------------------------------------------
# Public sign / verify primitives
# ---------------------------------------------------------------------------


def sign(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    charter_url: str,
    private_key: Ed25519PrivateKey,
    key_id: str,
    created: int | None = None,
) -> dict[str, str]:
    """Compute Signature + Signature-Input + Content-Digest for a request.

    Returns the THREE added headers as a fresh dict; the caller is
    responsible for merging them into the outbound request. We do not
    mutate `headers`.

    If `body` is non-empty, `content-digest` is added to the covered
    components AND emitted as its own header so the receiver can verify
    it. For empty bodies we omit both (RFC 9421 §2.1 allows this).
    """
    created_ts = created if created is not None else int(time.time())

    headers_lower: dict[str, str] = {k.lower(): v for k, v in headers.items()}
    added: dict[str, str] = {}

    covered: list[str] = list(_DERIVED_COMPONENTS)
    if body:
        digest = compute_content_digest(body)
        headers_lower["content-digest"] = digest
        added["Content-Digest"] = digest
        covered.append("content-digest")

    sig_params = _signature_params_value(
        covered,
        keyid=key_id,
        created=created_ts,
        charter_url=charter_url,
    )
    base = _build_signature_base(
        method=method,
        url=url,
        headers_lower=headers_lower,
        covered_components=covered,
        signature_params_value=sig_params,
    )
    signature_bytes = private_key.sign(base)
    sig_b64 = base64.b64encode(signature_bytes).decode("ascii")

    added["Signature-Input"] = f"{SIG_LABEL}={sig_params}"
    # Per RFC 9421 §4.1 the Signature header value is a dictionary of
    # byte-sequence sf-binary values: label=:b64:
    added["Signature"] = f"{SIG_LABEL}=:{sig_b64}:"
    return added


# ---------------------------------------------------------------------------
# Parsing Signature-Input / Signature back out
# ---------------------------------------------------------------------------


class SignatureInputParseError(ValueError):
    """Raised when Signature-Input / Signature headers are malformed."""


def parse_signature_input(header_value: str) -> tuple[str, list[str], dict[str, str]]:
    """Return (label, covered_components, params_dict) from a Signature-Input value.

    Only handles the single-signature shape Charter emits. Parameters are
    returned as their post-unquoted Python values; the only typed coercion
    is `created` → str (caller can int() if needed).

    Examples accepted (whitespace tolerant)::

        charter=("@method" "@path");keyid="abc"; created=123; alg="ed25519"
    """
    raw = header_value.strip()
    if "=" not in raw:
        raise SignatureInputParseError("missing label=")
    label, rest = raw.split("=", 1)
    label = label.strip()
    rest = rest.strip()
    if not rest.startswith("("):
        raise SignatureInputParseError("missing inner-list")
    end = rest.find(")")
    if end == -1:
        raise SignatureInputParseError("inner-list not closed")
    inner = rest[1:end].strip()
    params_str = rest[end + 1 :].lstrip().lstrip(";").strip()

    covered: list[str] = []
    if inner:
        # Inner list items are sf-strings separated by whitespace.
        for token in _split_inner_list(inner):
            covered.append(_parse_sf_string(token))

    params: dict[str, str] = {}
    if params_str:
        for chunk in _split_param_list(params_str):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                # Boolean param. Not used by Charter; record as empty.
                params[chunk.strip()] = ""
                continue
            k, v = chunk.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith('"'):
                params[k] = _parse_sf_string(v)
            else:
                params[k] = v
    return label, covered, params


def _split_inner_list(inner: str) -> list[str]:
    """Split an inner-list body into its token strings, respecting quotes."""
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    escape = False
    for ch in inner:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\" and in_quote:
            buf.append(ch)
            escape = True
            continue
        if ch == '"':
            buf.append(ch)
            in_quote = not in_quote
            continue
        if ch.isspace() and not in_quote:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _split_param_list(params_str: str) -> list[str]:
    """Split a `;`-separated parameter list, respecting quoted strings."""
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    escape = False
    for ch in params_str:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\" and in_quote:
            buf.append(ch)
            escape = True
            continue
        if ch == '"':
            buf.append(ch)
            in_quote = not in_quote
            continue
        if ch == ";" and not in_quote:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def parse_signature_header(header_value: str) -> tuple[str, bytes]:
    """Return (label, signature_bytes) from a Signature header value.

    Format: ``label=:<base64>:`` per RFC 9421 §4.1.
    """
    raw = header_value.strip()
    if "=" not in raw:
        raise SignatureInputParseError("missing label=")
    label, rest = raw.split("=", 1)
    label = label.strip()
    rest = rest.strip()
    if not (rest.startswith(":") and rest.endswith(":")):
        raise SignatureInputParseError("signature is not sf-binary (:b64:)")
    try:
        return label, base64.b64decode(rest[1:-1])
    except Exception as e:
        raise SignatureInputParseError(f"signature is not valid base64: {e}") from e


def verify(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    signature_bytes: bytes,
    covered_components: list[str],
    signature_params_value: str,
    public_key: Ed25519PublicKey,
) -> bool:
    """Recompute the signature base and verify the Ed25519 signature.

    `signature_params_value` is the EXACT string that appeared after the
    label in `Signature-Input`. It must be byte-identical to what the
    signer used, otherwise the signature base will not match.

    If `content-digest` is in `covered_components`, the body's actual
    digest is checked against the header value as well — RFC 9421 only
    binds the digest header into the signature, it does not by itself
    bind the body.
    """
    headers_lower: dict[str, str] = {k.lower(): v for k, v in headers.items()}

    if "content-digest" in covered_components:
        header_digest = headers_lower.get("content-digest")
        if header_digest is None:
            return False
        if not _verify_content_digest(header_digest, body):
            return False

    try:
        base = _build_signature_base(
            method=method,
            url=url,
            headers_lower=headers_lower,
            covered_components=covered_components,
            signature_params_value=signature_params_value,
        )
    except ValueError:
        return False

    try:
        public_key.verify(signature_bytes, base)
        return True
    except InvalidSignature:
        return False


def extract_signature_params_value(signature_input_header: str) -> str:
    """Return the substring after `<label>=` from a Signature-Input header.

    Used by the verifier to reconstruct the byte-identical params value
    that went into the signature base.
    """
    raw = signature_input_header.strip()
    if "=" not in raw:
        raise SignatureInputParseError("missing label=")
    _, rest = raw.split("=", 1)
    return rest.strip()


__all__ = [
    "SIG_LABEL",
    "SignatureInputParseError",
    "compute_content_digest",
    "extract_signature_params_value",
    "parse_signature_header",
    "parse_signature_input",
    "sign",
    "verify",
]
