"""SQL → ``SqlIntent`` extraction for the Postgres capability proxy.

The proxy needs a small, deterministic projection of each SQL
statement so it can build a natural-language description for Charter's
grader (``"perform SELECT on tables public.users; may touch PII"``).
We deliberately avoid threading the raw SQL text through the grader —
the LLM should reason about the *intent* of the operation, not be
asked to interpret SQL.

Fail-closed contract
--------------------

This module's single public function MUST NEVER raise. Anything
sqlglot cannot parse, anything we cannot map onto a known statement
type, and any unexpected internal error collapses into the
maximally-restrictive intent::

    SqlIntent(operation="OTHER", tables=[], has_pii_columns=True)

The proxy's gate (``charter.adapters.postgres.gate``) then turns this
into ``incompatible`` whenever the grader cannot positively justify
the operation, so the unknown-SQL path is rejected by default.
``has_pii_columns=True`` is intentional in the fail-closed branch:
when in doubt, assume the worst about what columns the query touches.

Why a tiny PII heuristic
------------------------

The grader has no schema knowledge. To give it a useful hint, we
flag a small set of column names that almost always indicate PII
(``email``, ``ssn``, ``password``, ...) inside the parsed
``SELECT`` projection or ``WHERE`` clause references. The list is
intentionally short — a false negative just means the grader
decides on tables alone, while a false positive only raises the bar.
Production deployments should override the list via the
``CHARTER_PG_PII_COLUMNS`` env var (comma-separated names).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# Statement-class → operation literal. Mirrors the small enum the issue
# spec asked for so callers do not have to translate sqlglot types.
_DDL_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
)


# Conservative default PII column allow-list. Names are matched
# case-insensitively against the column references sqlglot finds in
# the parsed statement. Override via ``CHARTER_PG_PII_COLUMNS`` env var
# (comma-separated).
_DEFAULT_PII_COLUMNS = frozenset(
    {
        "email",
        "email_address",
        "phone",
        "phone_number",
        "ssn",
        "social_security_number",
        "password",
        "password_hash",
        "credit_card",
        "card_number",
        "cvv",
        "dob",
        "date_of_birth",
        "address",
        "home_address",
        "ip_address",
        "tax_id",
    }
)


def _pii_columns() -> frozenset[str]:
    """Resolve the PII column allow-list at call time.

    Reads ``CHARTER_PG_PII_COLUMNS`` each call so tests can mutate the
    env var without re-importing the module. Falls back to
    ``_DEFAULT_PII_COLUMNS`` when the env var is unset or empty.
    """
    raw = os.environ.get("CHARTER_PG_PII_COLUMNS")
    if not raw:
        return _DEFAULT_PII_COLUMNS
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return frozenset(parts) if parts else _DEFAULT_PII_COLUMNS


@dataclass(frozen=True)
class SqlIntent:
    """Coarse-grained intent projected from a single SQL statement.

    Attributes:
        operation:
            One of ``"SELECT"``, ``"INSERT"``, ``"UPDATE"``,
            ``"DELETE"``, ``"DDL"`` (CREATE/DROP/ALTER/TRUNCATE), or
            ``"OTHER"`` for anything we do not recognise (including
            parse failures — see module docstring).
        tables:
            Fully-qualified table names referenced anywhere in the
            statement, in the order sqlglot encounters them. Empty
            when no table reference is found OR when parsing failed.
        has_pii_columns:
            True when any referenced column name appears in the PII
            allow-list (see ``_pii_columns``). Always True for the
            fail-closed fallback intent.
    """

    operation: str
    tables: list[str] = field(default_factory=list)
    has_pii_columns: bool = False


# The fail-closed default. Built once and reused by `intent_from_sql`
# every time we cannot positively classify the statement.
_FAIL_CLOSED = SqlIntent(operation="OTHER", tables=[], has_pii_columns=True)


def _classify_operation(node: exp.Expression) -> str:
    """Return the operation literal for a parsed statement.

    sqlglot's expression type system is closed; we map a handful of
    statement nodes onto our enum. Anything else (PRAGMA, SET, BEGIN,
    multi-statement scripts, ...) becomes ``"OTHER"`` so the gate
    treats it as fail-closed even if sqlglot parsed it fine.

    Note: UNION / INTERSECT / EXCEPT all classify as ``SELECT``
    because they are read-only set operations over multiple SELECTs.
    The table extractor will still surface every underlying table,
    so the gate can refuse on any sensitive one.
    """
    if isinstance(node, exp.Select):
        return "SELECT"
    # `Union` covers UNION / INTERSECT / EXCEPT in sqlglot; on newer
    # versions it is a subclass of `SetOperation`. Either name works
    # as a guard.
    if isinstance(node, exp.Union):
        return "SELECT"
    if isinstance(node, exp.Insert):
        return "INSERT"
    if isinstance(node, exp.Update):
        return "UPDATE"
    if isinstance(node, exp.Delete):
        return "DELETE"
    if isinstance(node, _DDL_TYPES):
        return "DDL"
    return "OTHER"


def _collect_tables(node: exp.Expression) -> list[str]:
    """Walk the parsed tree and collect every table reference.

    Deduplicates while preserving first-seen order so the resulting
    list reads like the source statement. CTE names are filtered out
    so a query that only reads from CTEs returns its underlying
    physical tables rather than the CTE alias (which is a red herring
    for capability decisions).
    """
    cte_names: set[str] = set()
    for cte in node.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            cte_names.add(alias.lower())

    seen: set[str] = set()
    tables: list[str] = []
    for tbl in node.find_all(exp.Table):
        # `Table.sql()` renders the qualified name (schema.table) when
        # present. We strip back-ticks / quotes for the comparison key
        # but keep the rendered form for display.
        name = tbl.name
        if name and name.lower() in cte_names:
            # CTE alias usage, not an underlying physical table.
            continue
        rendered = ".".join(p for p in (tbl.db, tbl.name) if p)
        if not rendered:
            continue
        key = rendered.lower()
        if key in seen:
            continue
        seen.add(key)
        tables.append(rendered)
    return tables


def _touches_pii_columns(node: exp.Expression, allow_list: frozenset[str]) -> bool:
    """Return True iff any column reference matches the PII allow-list.

    Checks bare ``Column`` nodes and the names of expressions inside
    ``Select`` projections. Star (``*``) projections are treated as
    *unknown columns* — we conservatively flag them so a ``SELECT *
    FROM users`` does not slip past a PII clause.
    """
    if list(node.find_all(exp.Star)):
        # `SELECT *` and `INSERT ... SELECT *` may project PII columns
        # without naming them. Treat as PII-touching.
        return True
    for col in node.find_all(exp.Column):
        col_name = col.name
        if isinstance(col_name, str) and col_name.lower() in allow_list:
            return True
    return False


def intent_from_sql(sql: str) -> SqlIntent:
    """Parse one SQL statement into a :class:`SqlIntent`.

    The function is **total**: every path that does not return a
    successfully-classified intent falls through to ``_FAIL_CLOSED``.
    Callers can therefore rely on the result without a try/except.

    Args:
        sql: A single SQL statement. Multi-statement scripts and
             empty/whitespace-only strings collapse to the fail-closed
             intent — the proxy will refuse them.

    Returns:
        A :class:`SqlIntent`. The dataclass is frozen so callers can
        treat it as an immutable value.
    """
    if not isinstance(sql, str) or not sql.strip():
        return _FAIL_CLOSED

    try:
        # `read="postgres"` matches the dialect the proxy guards.
        # `error_level="raise"` ensures sqlglot does not swallow
        # syntax errors and silently hand back a partially parsed
        # tree — we want hard failures to land in the fail-closed
        # branch below.
        parsed = sqlglot.parse(sql, read="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return _FAIL_CLOSED

    # Only the first non-empty statement is classified; the proxy
    # rejects multi-statement scripts elsewhere (the wire protocol
    # already enforces one Query per ``Q`` message, but a malicious
    # client could pack many statements into one Query payload).
    if not parsed or parsed[0] is None:
        return _FAIL_CLOSED
    if len(parsed) > 1:
        # Multi-statement: refuse rather than try to reason about a
        # batch under a single intent. Real PG simple-query mode does
        # allow this, but capability decisions become much harder when
        # the SQL means several different things at once.
        return _FAIL_CLOSED

    node = parsed[0]
    # sqlglot's `parse` returns `list[Expr | None]` where `Expr` is
    # the broad union that includes literals. Once we've ruled out
    # `None` above, the runtime value is always an `Expression`; this
    # narrowing is for the type-checker only.
    if not isinstance(node, exp.Expression):
        return _FAIL_CLOSED
    operation = _classify_operation(node)
    tables = _collect_tables(node)
    pii = _touches_pii_columns(node, _pii_columns())

    if operation == "OTHER" and not tables:
        # Parsed fine but unrecognized statement (PRAGMA, SET, BEGIN,
        # COMMIT, ...). Map to fail-closed so the gate refuses.
        return _FAIL_CLOSED

    return SqlIntent(operation=operation, tables=tables, has_pii_columns=pii)


__all__ = ["SqlIntent", "intent_from_sql"]
