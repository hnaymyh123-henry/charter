"""Reference Python conformance runner for the Charter protocol.

Walks every JSON file under `--vectors-dir`, dispatches it to a handler
based on the `input.operation` field, compares the result against
`expected_output` (or asserts `expected_error` was raised), and exits 0
iff every vector passed.

Run from the repo root:

    python conformance/runners/python/run.py --vectors-dir conformance/vectors
    python conformance/runners/python/run.py --vectors-dir conformance/vectors --filter sign
    python conformance/runners/python/run.py --vectors-dir conformance/vectors --junit-xml out.xml

Dependencies: stdlib + pydantic + cryptography (already pulled in by the
charter package). The runner deliberately does NOT depend on pytest so it
can be invoked from CI / scripts / docs without extra plumbing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

# Add repo root to sys.path so `import charter` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from charter import privacy  # noqa: E402
from charter.chain import _verify_chain_strict  # noqa: E402
from charter.constants import TYPE_TO_DECISION, aggregate_decision  # noqa: E402
from charter.pins import fingerprint_of  # noqa: E402
from charter.schema import Charter  # noqa: E402
from charter.signing import (  # noqa: E402
    _canonical_bytes,
    kid_for_public_key,
    public_key_to_jwk,
    verify_charter,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class VectorResult:
    """One vector's outcome."""

    path: Path
    name: str
    spec_section: str
    passed: bool
    duration_s: float
    reason: str | None = None  # populated on failure


@dataclass
class RunSummary:
    """Aggregate result across the whole run."""

    results: list[VectorResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.total > 0


# ---------------------------------------------------------------------------
# Operation dispatchers — one per `input.operation` value used by vectors
# ---------------------------------------------------------------------------


def _op_canonical_bytes(input_: dict[str, Any]) -> dict[str, Any]:
    """SHA-256 of the canonical bytes of a Charter."""
    charter = Charter.model_validate(input_["charter"])
    payload = _canonical_bytes(charter)
    return {
        "canonical_bytes_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "canonical_bytes_length": len(payload),
    }


def _op_sign(input_: dict[str, Any]) -> dict[str, Any]:
    """Sign a Charter (without log side effect) under a fixed-seed private key."""
    charter = Charter.model_validate(input_["charter"])
    seed = bytes.fromhex(input_["private_key_seed_hex"])
    private = Ed25519PrivateKey.from_private_bytes(seed)
    # Inline-sign so we don't trigger the transparency log.
    if charter.provenance.issuer_kid is None:
        charter.provenance.issuer_kid = kid_for_public_key(
            charter.provenance.issuer_public_key
        )
    payload = _canonical_bytes(charter)
    sig = private.sign(payload)
    charter.provenance.issuer_signature = f"ed25519:{base64.b64encode(sig).decode('ascii')}"
    return {
        "issuer_signature": charter.provenance.issuer_signature,
        "issuer_kid": charter.provenance.issuer_kid,
    }


def _op_verify(input_: dict[str, Any]) -> dict[str, Any]:
    charter = Charter.model_validate(input_["charter"])
    return {"valid": verify_charter(charter)}


def _op_type_to_decision(input_: dict[str, Any]) -> dict[str, Any]:
    return {"local_decision": TYPE_TO_DECISION[input_["clause_type"]]}


def _op_aggregate_decision(input_: dict[str, Any]) -> dict[str, Any]:
    return {"aggregate_decision": aggregate_decision(input_["local_decisions"])}


def _op_verify_chain_strict(input_: dict[str, Any]) -> dict[str, Any]:
    child = Charter.model_validate(input_["child"])
    parent = Charter.model_validate(input_["parent"])
    return {"valid": _verify_chain_strict(child, parent)}


def _op_lifecycle_status(input_: dict[str, Any]) -> dict[str, Any]:
    """Compute the conformant-implementation-required policy classification."""
    charter = Charter.model_validate(input_["charter"])
    now = datetime.fromisoformat(input_["now_iso"])
    status = charter.lifecycle.status
    is_revoked = status == "revoked"
    is_superseded = status == "superseded" or charter.lifecycle.replaced_by is not None
    # "expired" is computed from valid_until even if status is still "active".
    is_expired = charter.lifecycle.valid_until < now and not is_revoked

    if is_revoked:
        classification = "incompatible"
    elif is_superseded and not is_revoked:
        classification = "redirect_to_successor"
    elif is_expired:
        classification = "needs_approval_or_incompatible"
    else:
        classification = "usable"

    return {
        "status": status,
        "is_expired": is_expired,
        "is_revoked": is_revoked,
        "is_superseded": is_superseded,
        "policy_classification": classification,
    }


def _op_public_key_to_jwk(input_: dict[str, Any]) -> dict[str, Any]:
    return public_key_to_jwk(input_["public_key"])


def _op_kid_for_public_key(input_: dict[str, Any]) -> dict[str, Any]:
    kid = kid_for_public_key(input_["public_key"])
    return {"kid": kid, "kid_length_chars": len(kid)}


def _op_pin_fingerprint(input_: dict[str, Any]) -> dict[str, Any]:
    fp = fingerprint_of(input_["public_key"])
    return {
        "fingerprint": fp,
        "hex_length_chars": len(fp) - len("sha256:"),
    }


def _op_verify_log_chain(input_: dict[str, Any]) -> dict[str, Any]:
    """Walk a list of entry dicts and verify the SHA-256 chain.

    Re-implements the spec-level walk so the runner doesn't need the
    Python `transparency.verify_chain()` which reads from disk.
    """
    entries: list[dict[str, Any]] = input_["entries"]
    GENESIS = "sha256:" + "0" * 64
    if not entries:
        return {"ok": True, "entries": 0, "head_hash": GENESIS}

    expected_prev = GENESIS
    for entry in entries:
        seq = entry["seq"]
        if entry["prev_hash"] != expected_prev:
            return {
                "ok": False,
                "broken_at_seq": seq,
            }
        without_hash = {k: v for k, v in entry.items() if k != "entry_hash"}
        recomputed = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(without_hash, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        if recomputed != entry["entry_hash"]:
            return {
                "ok": False,
                "broken_at_seq": seq,
            }
        expected_prev = entry["entry_hash"]

    return {"ok": True, "entries": len(entries), "head_hash": entries[-1]["entry_hash"]}


def _op_redact_clause(input_: dict[str, Any]) -> dict[str, Any]:
    salt = bytes.fromhex(input_["salt_hex"])
    spans = [tuple(s) for s in input_["private_spans"]]
    redacted, fields, disclosures = privacy.redact_clause(
        input_["clause_text"], spans, salt=salt  # type: ignore[arg-type]
    )
    return {
        "redacted_text": redacted,
        "private_fields": [f.model_dump(mode="json") for f in fields],
        "disclosures": [d.model_dump(mode="json") for d in disclosures],
    }


def _op_verify_disclosure(input_: dict[str, Any]) -> dict[str, Any]:
    d = privacy.Disclosure.model_validate(input_["disclosure"])
    return {"valid": privacy.verify_disclosure(d, input_["claimed_hash"])}


def _op_canonical_bytes_contains_substring(input_: dict[str, Any]) -> dict[str, Any]:
    charter = Charter.model_validate(input_["charter"])
    payload = _canonical_bytes(charter)
    return {"contains_substring": input_["substring"].encode() in payload}


def _op_canonical_bytes_invariant_check(input_: dict[str, Any]) -> dict[str, Any]:
    """Confirm transparency_log_id assignment doesn't change canonical bytes."""
    pre = Charter.model_validate(input_["charter_pre_assign"])
    pre_payload = _canonical_bytes(pre)
    post = Charter.model_validate(input_["charter_pre_assign"])
    post.provenance.transparency_log_id = input_["charter_post_assign_log_id"]
    post_payload = _canonical_bytes(post)
    return {
        "pre_sha256": "sha256:" + hashlib.sha256(pre_payload).hexdigest(),
        "post_sha256": "sha256:" + hashlib.sha256(post_payload).hexdigest(),
        "must_be_equal": pre_payload == post_payload,
    }


def _op_canonical_bytes_inequality(input_: dict[str, Any]) -> dict[str, Any]:
    a = Charter.model_validate(input_["charter_a"])
    b = Charter.model_validate(input_["charter_b"])
    a_payload = _canonical_bytes(a)
    b_payload = _canonical_bytes(b)
    return {
        "sha256_a": "sha256:" + hashlib.sha256(a_payload).hexdigest(),
        "sha256_b": "sha256:" + hashlib.sha256(b_payload).hexdigest(),
        "must_be_different": a_payload != b_payload,
    }


def _op_canonical_bytes_excludes_plaintext(input_: dict[str, Any]) -> dict[str, Any]:
    charter = Charter.model_validate(input_["charter"])
    payload = _canonical_bytes(charter)
    return {"plaintext_in_canonical_bytes": input_["plaintext"].encode() in payload}


_DISPATCH: dict[str, Any] = {
    "canonical_bytes": _op_canonical_bytes,
    "canonical_bytes_contains_substring": _op_canonical_bytes_contains_substring,
    "canonical_bytes_invariant_check": _op_canonical_bytes_invariant_check,
    "canonical_bytes_inequality": _op_canonical_bytes_inequality,
    "canonical_bytes_excludes_plaintext": _op_canonical_bytes_excludes_plaintext,
    "sign": _op_sign,
    "verify": _op_verify,
    "type_to_decision": _op_type_to_decision,
    "aggregate_decision": _op_aggregate_decision,
    "verify_chain_strict": _op_verify_chain_strict,
    "lifecycle_status": _op_lifecycle_status,
    "public_key_to_jwk": _op_public_key_to_jwk,
    "kid_for_public_key": _op_kid_for_public_key,
    "pin_fingerprint": _op_pin_fingerprint,
    "verify_log_chain": _op_verify_log_chain,
    "redact_clause": _op_redact_clause,
    "verify_disclosure": _op_verify_disclosure,
}


# ---------------------------------------------------------------------------
# Vector execution
# ---------------------------------------------------------------------------


def _load_vector(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"vector {path} is unreadable / invalid JSON: {e}") from e


def _validate_vector_schema(path: Path, vector: dict[str, Any]) -> None:
    """Make sure the vector itself conforms to SPEC.md Appendix A."""
    required = {"name", "spec_section", "input", "expected_output", "expected_error"}
    missing = required - set(vector.keys())
    if missing:
        raise RuntimeError(f"vector {path} missing required fields: {sorted(missing)}")
    if vector["expected_output"] is not None and vector["expected_error"] is not None:
        raise RuntimeError(
            f"vector {path} declares both expected_output and expected_error; "
            "exactly one must be set"
        )
    if not isinstance(vector["input"], dict) or "operation" not in vector["input"]:
        raise RuntimeError(
            f"vector {path} has no input.operation; cannot dispatch"
        )


def _run_one(path: Path) -> VectorResult:
    start = perf_counter()
    try:
        vector = _load_vector(path)
        _validate_vector_schema(path, vector)
    except RuntimeError as e:
        return VectorResult(
            path=path,
            name=path.name,
            spec_section="(malformed)",
            passed=False,
            duration_s=perf_counter() - start,
            reason=str(e),
        )

    name = vector["name"]
    spec_section = vector["spec_section"]
    op = vector["input"]["operation"]

    handler = _DISPATCH.get(op)
    if handler is None:
        return VectorResult(
            path=path,
            name=name,
            spec_section=spec_section,
            passed=False,
            duration_s=perf_counter() - start,
            reason=f"unknown operation {op!r}; runner needs a new handler",
        )

    try:
        actual = handler(vector["input"])
    except Exception as e:  # pragma: no cover - exercised by expected_error path
        if vector["expected_error"] == type(e).__name__:
            return VectorResult(
                path=path,
                name=name,
                spec_section=spec_section,
                passed=True,
                duration_s=perf_counter() - start,
            )
        return VectorResult(
            path=path,
            name=name,
            spec_section=spec_section,
            passed=False,
            duration_s=perf_counter() - start,
            reason=(
                f"raised {type(e).__name__}: {e}\n"
                f"expected_error={vector['expected_error']!r}\n"
                f"{traceback.format_exc()}"
            ),
        )

    if vector["expected_error"] is not None:
        return VectorResult(
            path=path,
            name=name,
            spec_section=spec_section,
            passed=False,
            duration_s=perf_counter() - start,
            reason=(
                f"expected_error={vector['expected_error']!r} but handler returned "
                f"{actual!r}"
            ),
        )

    if actual != vector["expected_output"]:
        return VectorResult(
            path=path,
            name=name,
            spec_section=spec_section,
            passed=False,
            duration_s=perf_counter() - start,
            reason=(
                "output mismatch\n"
                f"  expected: {json.dumps(vector['expected_output'], sort_keys=True)}\n"
                f"  actual:   {json.dumps(actual, sort_keys=True, default=str)}"
            ),
        )

    return VectorResult(
        path=path,
        name=name,
        spec_section=spec_section,
        passed=True,
        duration_s=perf_counter() - start,
    )


def _discover(vectors_dir: Path, pattern: str | None) -> list[Path]:
    """Find every vector JSON, optionally filtering by substring in relative path."""
    if not vectors_dir.exists():
        return []
    paths = sorted(
        p
        for p in vectors_dir.rglob("*.json")
        if p.name != "generation_metadata.json"
    )
    if pattern:
        paths = [p for p in paths if pattern in str(p.relative_to(vectors_dir))]
    return paths


def _emit_junit_xml(summary: RunSummary, out_path: Path) -> None:
    """JUnit XML for CI integration (Jenkins, GitHub Actions, etc.)."""
    testsuite = ET.Element(
        "testsuite",
        {
            "name": "charter-conformance",
            "tests": str(summary.total),
            "failures": str(summary.failed),
            "errors": "0",
        },
    )
    for r in summary.results:
        case = ET.SubElement(
            testsuite,
            "testcase",
            {
                "classname": r.spec_section,
                "name": r.name,
                "time": f"{r.duration_s:.4f}",
            },
        )
        if not r.passed:
            failure = ET.SubElement(case, "failure", {"message": r.reason or "fail"})
            failure.text = r.reason or "fail"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(testsuite).write(out_path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="charter-conformance-runner",
        description="Run the Charter conformance vectors against the Python implementation.",
    )
    parser.add_argument(
        "--vectors-dir",
        type=Path,
        required=True,
        help="Path to the conformance/vectors/ directory.",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Only run vectors whose relative path contains this substring.",
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        default=None,
        help="If set, emit JUnit XML to this path for CI consumption.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print one line per vector (PASS/FAIL) instead of just the summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    paths = _discover(args.vectors_dir, args.filter)
    if not paths:
        print(
            f"No vectors found under {args.vectors_dir}"
            + (f" matching filter {args.filter!r}" if args.filter else ""),
            file=sys.stderr,
        )
        return 2

    summary = RunSummary()
    for path in paths:
        result = _run_one(path)
        summary.results.append(result)
        if args.verbose or not result.passed:
            tag = "PASS" if result.passed else "FAIL"
            print(f"[{tag}] {path.relative_to(args.vectors_dir)}  ({result.duration_s * 1000:.1f}ms)")
            if not result.passed and result.reason:
                # Indent the failure reason so it's clearly part of the failing vector.
                for line in result.reason.splitlines():
                    print(f"       {line}")

    print()
    print(
        f"Conformance: {summary.passed}/{summary.total} vectors passed "
        f"({summary.failed} failed)"
    )

    if args.junit_xml is not None:
        _emit_junit_xml(summary, args.junit_xml)
        print(f"JUnit XML written to {args.junit_xml}")

    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
