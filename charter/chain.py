"""Charter Chain — multi-hop scope attenuation.

A Charter Chain is a sequence of Charters where each child claims to be a
*stricter* version of its parent. The chain enforces a simple invariant:

    every restriction in the parent must persist or tighten in the child;
    the child must not relax anything the parent forbade.

This is the agent-as-principal case. A corporate principal issues a
broad Charter to an assistant agent; the assistant in turn issues a
narrower Charter to a sub-agent it delegates to. Attenuation guarantees
the sub-agent cannot do anything the original human/org principal
forbade, even though the assistant is technically the issuer of the
sub-Charter.

v0.7 ships **string-based subset verification**: clause text is
compared by exact match or substring containment. This is conservative
(a clause that says the same thing in different words won't be
recognized as attenuating) but it has zero LLM cost and is fully
deterministic. Semantic subset checking is on the v0.8+ roadmap.

The decision logic is in `verify_chain`. The MCP-callable form that
walks a chain over the network is in `mcp_server.fetch_charter_chain`
(separate issue).
"""

from __future__ import annotations

from ._logging import get_logger
from .schema import Charter, Clause

_log = get_logger("charter.chain")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_chain(child: Charter, parent: Charter) -> bool:
    """True iff `child` is a valid attenuation of `parent`.

    Rules (all must hold):

      1. **`out_of_scope` is preserved or expanded.** Every
         `out_of_scope` clause in `parent` must be covered by some
         `out_of_scope` clause in `child` (text equality or child's text
         being a superstring of the parent's). Child may add new
         exclusions freely.

      2. **`approval_required` is preserved or expanded.** Same rule as
         out_of_scope.

      3. **`scope` is a subset.** Every `scope` clause in `child` must
         match some `scope` clause in `parent` by exact text equality
         (so the child cannot grant itself a capability the parent
         did not authorize). Child may have FEWER scope clauses than
         parent; that's fine — narrower scope is the whole point.

      4. **`attenuation_proof.parent_charter_id` matches** the actual
         parent's `charter_id` when present. The proof is optional, but
         when supplied it has to be honest.

    Non-rules:

      - `operational_limit`, `style`, `data_handling` are not checked
        in v0.7. A future iteration may add per-type rules; the
        conservative answer for now is that these don't gate
        attenuation.

    Emits one `charter.chain` log line per call with the outcome.
    """
    parent_id = parent.charter_id
    child_id = child.charter_id

    # 1. attenuation_proof.parent_charter_id check
    if child.attenuation_proof is not None:
        claimed_parent = child.attenuation_proof.parent_charter_id
        if claimed_parent != parent_id:
            _log.warning(
                "chain rejected: attenuation_proof.parent_charter_id mismatch",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "claimed_parent": claimed_parent,
                    "outcome": "proof_mismatch",
                },
            )
            return False

    # 2. out_of_scope coverage
    parent_oos = [c for c in parent.clauses if c.type == "out_of_scope"]
    child_oos = [c for c in child.clauses if c.type == "out_of_scope"]
    for pc in parent_oos:
        if not _any_covers(child_oos, pc):
            _log.warning(
                "chain rejected: child does not preserve parent's out_of_scope",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "uncovered_clause_id": pc.id,
                    "uncovered_clause_text": pc.text,
                    "outcome": "out_of_scope_relaxed",
                },
            )
            return False

    # 3. approval_required coverage
    parent_appr = [c for c in parent.clauses if c.type == "approval_required"]
    child_appr = [c for c in child.clauses if c.type == "approval_required"]
    for pc in parent_appr:
        if not _any_covers(child_appr, pc):
            _log.warning(
                "chain rejected: child does not preserve parent's approval_required",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "uncovered_clause_id": pc.id,
                    "uncovered_clause_text": pc.text,
                    "outcome": "approval_required_relaxed",
                },
            )
            return False

    # 4. scope subset
    parent_scope_texts = {c.text for c in parent.clauses if c.type == "scope"}
    for cc in child.clauses:
        if cc.type == "scope" and cc.text not in parent_scope_texts:
            _log.warning(
                "chain rejected: child has a scope clause not present in parent",
                extra={
                    "child_charter_id": child_id,
                    "parent_charter_id": parent_id,
                    "extra_clause_id": cc.id,
                    "extra_clause_text": cc.text,
                    "outcome": "scope_widened",
                },
            )
            return False

    _log.info(
        "chain verified",
        extra={
            "child_charter_id": child_id,
            "parent_charter_id": parent_id,
            "outcome": "ok",
        },
    )
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _covers(child_clause: Clause, parent_clause: Clause) -> bool:
    """True iff `child_clause` is at least as restrictive as `parent_clause`.

    v0.7 rule: text equality OR child's text is a superstring of
    parent's. Both are case-sensitive. The superstring rule lets a child
    say "Do not accept marketing copy OR cold-email campaigns" and have
    that count as covering parent's "Do not accept marketing copy".
    """
    pt = parent_clause.text.strip()
    ct = child_clause.text.strip()
    return pt == ct or pt in ct


def _any_covers(child_clauses: list[Clause], parent_clause: Clause) -> bool:
    return any(_covers(cc, parent_clause) for cc in child_clauses)
