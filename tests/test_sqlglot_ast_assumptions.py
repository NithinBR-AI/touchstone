"""
test_sqlglot_ast_assumptions.py — Task 2.8, Req 17

Verifies the AST shapes sqlglot produces for pinned version 30.29.0.
The SQLValidator matcher relies on these exact node types — if sqlglot
changes them in a future version, these tests will catch it before the
validator silently breaks.
"""

from __future__ import annotations

import pytest
import sqlglot
import sqlglot.expressions as exp

PINNED_VERSION = "30.29.0"


class TestVersion:
    def test_pinned_version(self) -> None:
        assert sqlglot.__version__ == PINNED_VERSION, (
            f"sqlglot version mismatch: expected {PINNED_VERSION}, "
            f"got {sqlglot.__version__}. "
            "Update test_sqlglot_ast_assumptions.py and re-verify all AST shapes."
        )


def parse_one(sql: str) -> exp.Expression:
    stmts = sqlglot.parse(sql, dialect="postgres")
    assert len(stmts) == 1
    assert stmts[0] is not None
    return stmts[0]


class TestCreateIndexAST:
    """CREATE INDEX shapes used by SQLValidator._validate_create_index."""

    def test_plain_create_index_is_create(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        assert isinstance(stmt, exp.Create)

    def test_index_arg_is_index_expression(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        assert isinstance(stmt.this, exp.Index)

    def test_table_name_extractable(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        index: exp.Index = stmt.this
        table = index.args.get("table")
        assert table is not None
        assert "users" in str(table).lower()

    def test_index_name_extractable(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        index: exp.Index = stmt.this
        name = index.args.get("this")
        assert name is not None
        assert "ix_users_email" in str(name).lower()

    def test_columns_accessible_via_find_all(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        index: exp.Index = stmt.this
        cols = list(index.find_all(exp.Column))
        assert len(cols) >= 1

    def test_multi_column_index(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_a_b ON users (a, b)")
        index: exp.Index = stmt.this
        cols = list(index.find_all(exp.Column))
        assert len(cols) == 2

    def test_unique_flag_present(self) -> None:
        stmt = parse_one("CREATE UNIQUE INDEX ix_users_email ON users (email)")
        assert stmt.args.get("unique") is True

    def test_no_unique_flag_on_plain(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        assert not stmt.args.get("unique")

    def test_partial_index_has_where(self) -> None:
        # In sqlglot 30.29.0 the WHERE lives inside IndexParameters, not Index
        stmt = parse_one(
            "CREATE INDEX ix_users_active ON users (email) WHERE active = true"
        )
        index: exp.Index = stmt.this
        params = index.args.get("params")
        assert params is not None
        assert params.args.get("where") is not None

    def test_plain_index_no_where(self) -> None:
        stmt = parse_one("CREATE INDEX ix_users_email ON users (email)")
        index: exp.Index = stmt.this
        params = index.args.get("params")
        where = params.args.get("where") if params else None
        assert where is None


class TestAlterTableTypeAST:
    """ALTER TABLE ... ALTER COLUMN ... TYPE shapes."""

    def test_alter_table_is_alter(self) -> None:
        # In sqlglot 30.29.0 the node is exp.Alter, NOT exp.AlterTable
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
        )
        assert isinstance(stmt, exp.Alter)

    def test_table_name_extractable(self) -> None:
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
        )
        assert "users" in str(stmt.this).lower()

    def test_alter_column_node_present(self) -> None:
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
        )
        alter_col = stmt.find(exp.AlterColumn)
        assert alter_col is not None

    def test_column_name_extractable(self) -> None:
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
        )
        alter_col = stmt.find(exp.AlterColumn)
        assert "email" in str(alter_col.this).lower()

    def test_dtype_present(self) -> None:
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
        )
        alter_col = stmt.find(exp.AlterColumn)
        dtype = alter_col.args.get("dtype")
        assert dtype is not None

    def test_using_clause_detectable(self) -> None:
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN score TYPE integer USING score::integer"
        )
        alter_col = stmt.find(exp.AlterColumn)
        assert alter_col.args.get("using") is not None

    def test_no_using_on_plain(self) -> None:
        stmt = parse_one(
            "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
        )
        alter_col = stmt.find(exp.AlterColumn)
        assert not alter_col.args.get("using")


class TestMultiStatementRejection:
    """sqlglot.parse returns multiple statements for semicolon-separated SQL."""

    def test_two_statements_parsed_as_two(self) -> None:
        stmts = [
            s for s in sqlglot.parse(
                "CREATE INDEX a ON t (x); CREATE INDEX b ON t (y)",
                dialect="postgres",
            )
            if s is not None
        ]
        assert len(stmts) == 2

    def test_single_statement_parsed_as_one(self) -> None:
        stmts = [
            s for s in sqlglot.parse(
                "CREATE INDEX a ON t (x)", dialect="postgres"
            )
            if s is not None
        ]
        assert len(stmts) == 1


class TestSetStatementAST:
    """SET statements should be detectable as Command nodes."""

    def test_set_is_command(self) -> None:
        stmt = parse_one("SET statement_timeout = 30000")
        # sqlglot may parse SET as a Command or a dedicated Set node
        assert isinstance(stmt, (exp.Command, exp.Set))

    def test_set_local_is_command_or_set(self) -> None:
        stmt = parse_one("SET LOCAL statement_timeout = 30000")
        assert isinstance(stmt, (exp.Command, exp.Set))


class TestDropStatementAST:
    """DROP statements must be detectable as exp.Drop."""

    def test_drop_table_is_drop(self) -> None:
        stmt = parse_one("DROP TABLE users")
        assert isinstance(stmt, exp.Drop)

    def test_drop_column_via_alter(self) -> None:
        # DROP COLUMN is an ALTER statement in sqlglot 30.29.0
        stmt = parse_one("ALTER TABLE users DROP COLUMN email")
        assert isinstance(stmt, exp.Alter)

    def test_drop_index_is_drop(self) -> None:
        stmt = parse_one("DROP INDEX ix_users_email")
        assert isinstance(stmt, exp.Drop)
