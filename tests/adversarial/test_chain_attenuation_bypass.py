"""Adversarial: chain attenuation bypass attempts.

Attack model: an intermediate agent issues a child Charter that claims
to be a stricter subset of its parent, but actually relaxes some
restriction in a way the v0.7 string-based `verify_chain` cannot catch.

Defense (ADR-010): v0.7 ships string-based subset verification. Clauses
are compared by exact text equality OR child's text being a superstring
of parent's. This is fully deterministic and zero-cost but conservative;
two big gaps are documented here:

  1. Synonym / paraphrase: child says the same thing in different
     words. String check FAILS the chain (false negative — the chain is
     actually fine). Not a security issue, just a usability one.

  2. Reversed-meaning superstring: child's text contains the parent's
     text as a substring, but the surrounding words negate or qualify
     the prohibition. The string check INCORRECTLY ACCEPTS the chain
     (false positive — this is the dangerous attack vector). Flagged
     xfail and linked to Issue #26 for semantic chain checking.

These tests assert what the current string check catches and what it
provably misses, with the gaps marked `xfail` so they show up in CI as
known limitations rather than silent successes.
"""

from __future__ import annotations

from typing import Any

import pytest

from charter.chain import verify_chain
from charter.schema import Clause

# ---------------------------------------------------------------------------
# Cases the v0.7 string check correctly catches
# ---------------------------------------------------------------------------


def test_child_drops_parent_oos_caught(charter_factory: Any) -> None:
    """Baseline: child silently drops an out_of_scope clause the parent
    declared. String check catches this — the parent clause text is not
    found in any of the child's out_of_scope clauses."""
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[
            Clause(id="P-001", type="scope", text="Accounting."),
            Clause(id="P-002", type="out_of_scope", text="Do not write code."),
        ],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting."),
            # Parent's out_of_scope is missing entirely.
        ],
    )
    assert verify_chain(child, parent) is False


def test_child_adds_scope_not_in_parent_caught(charter_factory: Any) -> None:
    """Attack: child grants itself an extra capability the parent did
    not authorize. String check catches this via the scope-subset rule."""
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[Clause(id="P-001", type="scope", text="Accounting only.")],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[
            Clause(id="C-001", type="scope", text="Accounting only."),
            Clause(id="C-002", type="scope", text="Marketing too."),  # widened
        ],
    )
    assert verify_chain(child, parent) is False


def test_child_swaps_oos_text_with_truthful_synonym_fails_string_check(
    charter_factory: Any,
) -> None:
    """Documented limitation #1 (false negative): child rephrases the
    parent's restriction in different words but keeps the same meaning.
    The chain SHOULD be accepted (it is genuinely as strict as the
    parent), but the v0.7 string check rejects it.

    This is a usability issue, not a security one — the false negative
    direction is "be too restrictive". Asserted here so any future
    relaxation of the string rule has to consciously change this test.
    """
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[
            Clause(id="P-001", type="scope", text="Coding."),
            Clause(id="P-002", type="out_of_scope", text="Marketing content."),
        ],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[
            Clause(id="C-001", type="scope", text="Coding."),
            # Semantically identical, but the words differ.
            Clause(id="C-002", type="out_of_scope", text="Advertising material."),
        ],
    )
    # Rejected: 'Marketing content' is not a substring of 'Advertising
    # material'. Conservative but documented.
    assert verify_chain(child, parent) is False


def test_child_keyword_overlap_without_negation_still_caught(
    charter_factory: Any,
) -> None:
    """Adversarial attempt where child's out_of_scope shares a keyword
    with parent's but is much narrower (so the parent restriction is
    actually weaker in child). String check catches this because the
    parent's full text is not a substring of the child's."""
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[
            Clause(
                id="P-002",
                type="out_of_scope",
                text="Do not export customer PII to any third party.",
            )
        ],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[
            Clause(
                id="C-002",
                type="out_of_scope",
                # Narrower: only forbids one specific party — leaves the
                # rest open. String check catches the relaxation.
                text="Do not export PII to ad networks.",
            )
        ],
    )
    assert verify_chain(child, parent) is False


# ---------------------------------------------------------------------------
# Documented limitation #2: reversed-meaning superstring (xfail, security gap)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known v0.7 string-check limitation: child's text contains the "
        "parent's restriction as a substring but the surrounding words "
        "negate it. The superstring rule accepts the chain even though "
        "the child has effectively removed the restriction. Tracked in "
        "Issue #26 (semantic chain verification)."
    ),
)
def test_child_superstring_with_negating_qualifier_should_fail_but_passes(
    charter_factory: Any,
) -> None:
    """Real attack vector. v0.7 superstring rule accepts the chain
    incorrectly. Once Issue #26 lands and verify_chain consults a
    semantic checker, this test should pass without the xfail."""
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[
            Clause(id="P-001", type="scope", text="Editorial review."),
            Clause(
                id="P-002",
                type="out_of_scope",
                text="Do not write code.",
            ),
        ],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[
            Clause(id="C-001", type="scope", text="Editorial review."),
            Clause(
                id="C-002",
                type="out_of_scope",
                # Contains 'Do not write code.' as a substring but the
                # 'except' clause flips the meaning.
                text=(
                    "Do not write code. (Exception: technical articles "
                    "may include illustrative code snippets and full "
                    "demo programs.)"
                ),
            ),
        ],
    )
    # Asserts the SAFE answer (chain should be rejected). Currently
    # fails because the string rule accepts the superstring.
    assert verify_chain(child, parent) is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known v0.7 string-check limitation: child appends an additional "
        "qualifier sentence after the parent's restriction. The parent's "
        "text appears verbatim as a prefix, so the superstring rule "
        "treats the child as 'at least as strict' — but the extra sentence "
        "carves out exceptions in practice. Issue #26 follow-up."
    ),
)
def test_child_appended_carve_out_sentence_should_fail_but_passes(
    charter_factory: Any,
) -> None:
    """Variant of the superstring attack with an appended carve-out
    sentence. The parent's clause appears verbatim at the start; the
    appended sentence effectively removes the restriction in practice."""
    parent = charter_factory(
        charter_id="parent:1",
        clauses=[
            Clause(
                id="P-002",
                type="out_of_scope",
                text="Production database writes are forbidden.",
            )
        ],
    )
    child = charter_factory(
        charter_id="child:1",
        clauses=[
            Clause(
                id="C-002",
                type="out_of_scope",
                # Parent's full sentence is present verbatim, so the
                # superstring rule treats the child as covering it.
                # The appended sentence neutralises the prohibition.
                text=(
                    "Production database writes are forbidden. "
                    "However, this restriction is waived for any operation "
                    "that the sub-agent deems necessary."
                ),
            )
        ],
    )
    assert verify_chain(child, parent) is False
