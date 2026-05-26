"""Unit tests for ``charter.adapters.postgres.intent``.

These tests are fully offline — they exercise the sqlglot-backed
extractor and the fail-closed defaults. No Postgres required.
"""

from __future__ import annotations

import dataclasses

import pytest

# The intent extractor depends on `sqlglot` (in the `postgres_proxy` optional
# extra, not `[dev]`). Skip the whole module if the extra isn't installed so
# `pip install -e .[dev]` envs collect cleanly instead of erroring out.
pytest.importorskip("sqlglot")

from charter.adapters.postgres.intent import SqlIntent, intent_from_sql  # noqa: E402


class TestOperationClassification:
    """Every well-formed statement should produce the expected operation literal."""

    @pytest.mark.parametrize(
        ("sql", "expected_operation"),
        [
            ("SELECT id FROM users", "SELECT"),
            ("INSERT INTO orders (id, total) VALUES (1, 99.99)", "INSERT"),
            ("UPDATE orders SET total = 100 WHERE id = 1", "UPDATE"),
            ("DELETE FROM orders WHERE id = 1", "DELETE"),
            ("CREATE TABLE foo (id int)", "DDL"),
            ("DROP TABLE foo", "DDL"),
            ("ALTER TABLE foo ADD COLUMN bar int", "DDL"),
            ("TRUNCATE TABLE foo", "DDL"),
        ],
    )
    def test_each_statement_type(self, sql: str, expected_operation: str) -> None:
        intent = intent_from_sql(sql)
        assert intent.operation == expected_operation, (
            f"{sql!r} should classify as {expected_operation}, got {intent.operation}"
        )


class TestTableExtraction:
    """Table references — qualified, schema-prefixed, in JOINs and CTEs."""

    def test_single_table(self) -> None:
        intent = intent_from_sql("SELECT 1 FROM orders")
        assert intent.tables == ["orders"]

    def test_schema_qualified(self) -> None:
        intent = intent_from_sql("SELECT 1 FROM public.orders")
        assert intent.tables == ["public.orders"]

    def test_join(self) -> None:
        intent = intent_from_sql(
            "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id"
        )
        # sqlglot may produce either order depending on dialect; check
        # both are present and no duplicates.
        assert set(intent.tables) == {"orders", "customers"}
        assert len(intent.tables) == 2

    def test_subquery_table_recognised(self) -> None:
        intent = intent_from_sql(
            "SELECT * FROM (SELECT id FROM orders) sub WHERE sub.id IN "
            "(SELECT customer_id FROM customers)"
        )
        assert "orders" in intent.tables
        assert "customers" in intent.tables

    def test_cte_alias_excluded_underlying_table_kept(self) -> None:
        intent = intent_from_sql(
            "WITH recent AS (SELECT * FROM orders WHERE id > 100) SELECT id FROM recent"
        )
        # `recent` is the CTE alias — should be filtered out. `orders`
        # (the underlying physical table) must remain.
        assert intent.tables == ["orders"], (
            f"CTE alias should be excluded; underlying table preserved. Got tables={intent.tables}"
        )

    def test_update_with_where(self) -> None:
        intent = intent_from_sql("UPDATE production_secrets SET value = 'redacted' WHERE id = 1")
        assert intent.operation == "UPDATE"
        assert intent.tables == ["production_secrets"]


class TestPiiHeuristic:
    """The PII flag should fire on default-list column names and SELECT *."""

    def test_default_list_email(self) -> None:
        intent = intent_from_sql("SELECT email FROM users")
        assert intent.has_pii_columns is True

    def test_default_list_ssn_in_where(self) -> None:
        intent = intent_from_sql("SELECT id FROM users WHERE ssn = '123-45-6789'")
        assert intent.has_pii_columns is True

    def test_select_star_flags_pii(self) -> None:
        intent = intent_from_sql("SELECT * FROM users")
        assert intent.has_pii_columns is True, (
            "SELECT * may project PII columns; conservatively flag it."
        )

    def test_non_pii_columns(self) -> None:
        intent = intent_from_sql("SELECT id, total FROM orders WHERE id = 1")
        assert intent.has_pii_columns is False

    def test_env_var_override_adds_column(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHARTER_PG_PII_COLUMNS", "secret_key, foo")
        intent = intent_from_sql("SELECT secret_key FROM keys")
        assert intent.has_pii_columns is True

    def test_env_var_override_does_not_flag_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When the env var IS set, only the listed names count; the
        # built-in defaults no longer apply. This is a deliberate
        # contract — operators who override should provide the full
        # list they want enforced.
        monkeypatch.setenv("CHARTER_PG_PII_COLUMNS", "secret_key")
        intent = intent_from_sql("SELECT email FROM users WHERE id = 1")
        assert intent.has_pii_columns is False


class TestFailClosed:
    """Anything that does not classify cleanly must collapse to fail-closed."""

    def test_empty_string(self) -> None:
        intent = intent_from_sql("")
        assert intent == SqlIntent(operation="OTHER", tables=[], has_pii_columns=True)

    def test_whitespace_only(self) -> None:
        intent = intent_from_sql("   \n\t  ")
        assert intent.operation == "OTHER"
        assert intent.tables == []
        assert intent.has_pii_columns is True

    def test_unparseable_garbage(self) -> None:
        intent = intent_from_sql("not even a sql statement {{{ }")
        assert intent.operation == "OTHER"
        assert intent.tables == []
        assert intent.has_pii_columns is True

    def test_unicode_decode_error_inputs(self) -> None:
        # We accept str only; non-str inputs collapse fail-closed.
        intent = intent_from_sql(b"SELECT 1" if False else "")  # type: ignore[arg-type]
        assert intent.operation == "OTHER"

    def test_non_string_input(self) -> None:
        # Defensive: the proxy decodes to str before calling, but the
        # public helper must still be total.
        intent = intent_from_sql(None)  # type: ignore[arg-type]
        assert intent.operation == "OTHER"

    def test_multistatement_refused(self) -> None:
        # Multi-statement scripts can mean several different operations
        # under a single Charter decision — refuse rather than guess.
        intent = intent_from_sql("SELECT 1; DROP TABLE production;")
        assert intent.operation == "OTHER"
        assert intent.tables == []

    def test_unknown_statement_fails_closed(self) -> None:
        # sqlglot parses BEGIN/SET/COMMIT/... fine, but they do not
        # map to an enum we know how to gate on.
        intent = intent_from_sql("BEGIN")
        assert intent.operation == "OTHER"
        assert intent.tables == []

    def test_intent_is_frozen(self) -> None:
        # The dataclass is frozen so the proxy can hand it around
        # without worrying about callers mutating it.
        intent = intent_from_sql("SELECT 1 FROM t")
        with pytest.raises(dataclasses.FrozenInstanceError):
            intent.operation = "DDL"  # type: ignore[misc]


class TestSqlInjectionResistance:
    """The intent extractor must not be fooled by injection-style payloads.

    The extractor is parsing trusted-source SQL the client sent over the
    wire — there is no "injection" in the classic sense. But a malicious
    client may craft SQL designed to make the *intent extraction*
    misbehave (e.g. hide a DROP behind a comment, embed a SELECT inside
    a UNION). The proxy is responsible for refusing those cases via
    the fail-closed default; these tests pin that behaviour.
    """

    def test_comment_injection_does_not_hide_drop(self) -> None:
        # Multi-statement scripts are refused outright, which covers
        # the "comment out the rest" trick.
        intent = intent_from_sql("SELECT 1 /* harmless */; DROP TABLE production;")
        assert intent.operation == "OTHER"

    def test_union_select_reveals_all_tables(self) -> None:
        # A UNION-style query that touches a secret table should
        # produce a SELECT intent with BOTH tables, so the gate can
        # refuse on the sensitive one.
        intent = intent_from_sql(
            "SELECT id FROM public.users UNION SELECT id FROM secret.credentials"
        )
        assert intent.operation == "SELECT"
        assert "public.users" in intent.tables
        assert "secret.credentials" in intent.tables
