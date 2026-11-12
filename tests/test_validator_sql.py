"""
Test the SQL validator — the safety boundary.

Tests:
  1. Operation matrix mapping (positive + negative)
  2. Single-statement / SET / USING rejection
  3. Forbidden operations (DROP, DELETE, TRUNCATE)
  4. Canonical target binding (table.column -> SQL AST)
  5. Type widening verification
  6. Index validation (UNIQUE, expression, partial)
  7. Binding mismatch detection
"""

import pytest
import sqlglot
import sqlglot.expressions as exp

from core.models import Difference, GhostState, SQLChange
from core.validator import SQLValidator, ValidationResult

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def validator() -> SQLValidator:
    return SQLValidator()


@pytest.fixture
def index_missing_diff() -> Difference:
    return Difference(
        kind="INDEX_MISSING",
        target="ix_users_email",
        context={
            "table": "users",
            "columns": ["email"],
            "original_actual_type": None,
            "original_intended_type": None,
        },
    )


@pytest.fixture
def type_changed_diff() -> Difference:
    return Difference(
        kind="TYPE_CHANGED",
        target="users.age",
        context={
            "table": "users",
            "column": "age",
            "original_actual_type": "integer",
            "original_intended_type": "bigint",
        },
    )


# --------------------------------------------------------------------------
# 1. Operation matrix
# --------------------------------------------------------------------------


def test_operation_matrix_positive(
    validator: SQLValidator, index_missing_diff: Difference
):
    """Valid CREATE INDEX passes."""
    change = SQLChange(
        statement="CREATE INDEX ix_users_email ON users (email)",
        operation="CREATE_INDEX",
        target="ix_users_email",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "valid"
    assert result.ghost_state == GhostState.PENDING


def test_operation_matrix_blocked_kind(validator: SQLValidator):
    """TABLE_MISSING is REVIEW_REQUIRED (not auto-fixable) regardless of SQL."""
    diff = Difference(kind="TABLE_MISSING", target="new_table", context={})
    change = SQLChange(
        statement="CREATE TABLE new_table (id SERIAL PRIMARY KEY)",
        operation="CREATE_TABLE",
        target="new_table",
    )
    result = validator.validate(change, diff)
    # TABLE_MISSING has auto_fixable=False in matrix, so it goes to REVIEW_REQUIRED
    assert result.status == "review_required"
    assert "not auto-fixable" in result.reason
    assert result.ghost_state == GhostState.REVIEW_REQUIRED


def test_operation_matrix_review_required(validator: SQLValidator):
    """COLUMN_MISSING is REVIEW_REQUIRED."""
    diff = Difference(kind="COLUMN_MISSING", target="users.middle_name", context={})
    change = SQLChange(
        statement="ALTER TABLE users ADD COLUMN middle_name VARCHAR(100)",
        operation="ADD_COLUMN",
        target="users.middle_name",
    )
    result = validator.validate(change, diff)
    assert result.status == "review_required"
    assert "not auto-fixable" in result.reason
    assert result.ghost_state == GhostState.REVIEW_REQUIRED


# --------------------------------------------------------------------------
# 2. SQL parsing and structural rules
# --------------------------------------------------------------------------


def test_single_statement_rule(validator: SQLValidator, index_missing_diff: Difference):
    """Multi-statement SQL is rejected."""
    change = SQLChange(
        statement="CREATE INDEX ix_users_email ON users (email); CREATE INDEX ix2 ON users (name)",
        operation="CREATE_INDEX",
        target="ix_users_email",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "expected exactly 1 statement" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_set_statement_rejected(validator: SQLValidator):
    """SET statements are forbidden regardless of diff type."""
    # Use TYPE_CHANGED diff since that's what the validator checks first for SET
    diff = Difference(
        kind="TYPE_CHANGED",
        target="users.age",
        context={"original_actual_type": "integer", "original_intended_type": "bigint"},
    )
    change = SQLChange(
        statement="SET statement_timeout = '30s'",
        operation="SET_STATEMENT",
        target="timeout",
    )
    result = validator.validate(change, diff)
    assert result.status == "invalid"
    assert "SET statements are forbidden" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_forbidden_node_types(validator: SQLValidator):
    """DROP, DELETE, TRUNCATE are rejected."""
    for forbidden_sql in [
        "DROP TABLE users",
        "DELETE FROM users",
        "TRUNCATE TABLE users",
        "DROP INDEX ix_users_email",
    ]:
        diff = Difference(kind="INDEX_MISSING", target="ix_users_email", context={})
        change = SQLChange(
            statement=forbidden_sql, operation="DROP", target="ix_users_email"
        )
        result = validator.validate(change, diff)
        assert result.status == "blocked"
        assert "forbidden SQL operation" in result.reason
        assert result.ghost_state == GhostState.BLOCKED


# --------------------------------------------------------------------------
# 3. CREATE INDEX validation
# --------------------------------------------------------------------------


def test_create_index_unique_rejected(
    validator: SQLValidator, index_missing_diff: Difference
):
    """UNIQUE indexes are forbidden."""
    change = SQLChange(
        statement="CREATE UNIQUE INDEX ix_users_email ON users (email)",
        operation="CREATE_INDEX",
        target="ix_users_email",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "UNIQUE indexes are not auto-fixable" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_create_index_expression_rejected(
    validator: SQLValidator, index_missing_diff: Difference
):
    """Expression indexes are forbidden."""
    change = SQLChange(
        statement="CREATE INDEX idx ON users (LOWER(email))",
        operation="CREATE_INDEX",
        target="idx",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "expression indexes are forbidden" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_create_index_partial_rejected(
    validator: SQLValidator, index_missing_diff: Difference
):
    """Partial indexes (WHERE) are forbidden."""
    # sqlglot 30.29.0 represents WHERE clause in IndexParameters
    change = SQLChange(
        statement="CREATE INDEX ix_active_users ON users (email) WHERE active = true",
        operation="CREATE_INDEX",
        target="ix_active_users",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "partial indexes (WHERE) are forbidden" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_create_index_name_mismatch(
    validator: SQLValidator, index_missing_diff: Difference
):
    """Index name must match difference target."""
    change = SQLChange(
        statement="CREATE INDEX wrong_name ON users (email)",
        operation="CREATE_INDEX",
        target="wrong_name",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "does not match difference target" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


# --------------------------------------------------------------------------
# 4. ALTER COLUMN TYPE validation
# --------------------------------------------------------------------------


def test_alter_type_us_clause_rejected(
    validator: SQLValidator, type_changed_diff: Difference
):
    """USING clause is forbidden."""
    change = SQLChange(
        statement="ALTER TABLE users ALTER COLUMN age TYPE bigint USING (age::bigint)",
        operation="ALTER_COLUMN_TYPE",
        target="users.age",
    )
    result = validator.validate(change, type_changed_diff)
    assert result.status == "invalid"
    assert "USING clause is forbidden" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_alter_type_target_binding(validator: SQLValidator):
    """SQL must target the same table.column as the difference."""
    diff = Difference(
        kind="TYPE_CHANGED",
        target="users.age",
        context={"original_actual_type": "integer", "original_intended_type": "bigint"},
    )
    change = SQLChange(
        statement="ALTER TABLE orders ALTER COLUMN total TYPE bigint",
        operation="ALTER_COLUMN_TYPE",
        target="orders.total",
    )
    result = validator.validate(change, diff)
    assert result.status == "invalid"
    assert (
        "targets 'users.age'" in result.reason
        or "targets 'orders.total'" in result.reason
    )
    assert result.ghost_state == GhostState.BLOCKED


# --------------------------------------------------------------------------
# 5. Type widening verification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old_type, new_type",
    [
        ("smallint", "integer"),
        ("integer", "bigint"),
        ("real", "double precision"),
        ("varchar(10)", "varchar(20)"),
        ("varchar(10)", "text"),
    ],
)
def test_widening_pairs_allowed(validator: SQLValidator, old_type: str, new_type: str):
    """Allowed widening pairs pass."""
    diff = Difference(
        kind="TYPE_CHANGED",
        target="users.column",
        context={
            "original_actual_type": old_type,
            "original_intended_type": new_type,
        },
    )
    change = SQLChange(
        statement=f"ALTER TABLE users ALTER COLUMN column TYPE {new_type}",
        operation="ALTER_COLUMN_TYPE",
        target="users.column",
    )
    result = validator.validate(change, diff)
    assert result.status == "valid"
    assert result.ghost_state == GhostState.PENDING


@pytest.mark.parametrize(
    "old_type, new_type",
    [
        ("integer", "smallint"),  # narrowing
        ("bigint", "integer"),  # narrowing
        ("double precision", "real"),  # narrowing
        ("varchar(20)", "varchar(10)"),  # narrowing
        ("text", "varchar(100)"),  # text → varchar not in matrix
        ("timestamp", "timestamptz"),  # not in matrix
        ("numeric", "integer"),  # not in matrix
    ],
)
def test_non_widening_review_required(
    validator: SQLValidator, old_type: str, new_type: str
):
    """Non-widening pairs yield REVIEW_REQUIRED."""
    diff = Difference(
        kind="TYPE_CHANGED",
        target="users.column",
        context={
            "original_actual_type": old_type,
            "original_intended_type": new_type,
        },
    )
    change = SQLChange(
        statement=f"ALTER TABLE users ALTER COLUMN column TYPE {new_type}",
        operation="ALTER_COLUMN_TYPE",
        target="users.column",
    )
    result = validator.validate(change, diff)
    assert result.status == "review_required"
    assert "not a permitted widening pair" in result.reason
    assert result.ghost_state == GhostState.REVIEW_REQUIRED


def test_widening_verification_from_context_not_rationale(validator: SQLValidator):
    """Validator reads old/new type from Difference.context, not from SQL."""
    diff = Difference(
        kind="TYPE_CHANGED",
        target="users.column",
        context={
            "original_actual_type": "integer",
            "original_intended_type": "bigint",
        },
    )
    # SQL says smallint → integer, but context says integer → bigint
    change = SQLChange(
        statement="ALTER TABLE users ALTER COLUMN column TYPE integer",
        operation="ALTER_COLUMN_TYPE",
        target="users.column",
    )
    result = validator.validate(change, diff)
    assert result.status == "review_required"
    assert "'integer'" in result.reason
    assert result.ghost_state == GhostState.REVIEW_REQUIRED


# --------------------------------------------------------------------------
# 6. Canonical target binding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql, target, should_pass",
    [
        # Quoted preserves case
        ('CREATE INDEX "IX_Users_Email" ON "Users" ("Email")', "IX_Users_Email", True),
        # Unquoted lowercases
        ("CREATE INDEX ix_users_email ON users (email)", "IX_USERS_EMAIL", True),
        # Schema stripping
        ("CREATE INDEX ix_users_email ON public.users (email)", "ix_users_email", True),
        # Non-public schema rejected
        ("CREATE INDEX ix_users_email ON auth.users (email)", "ix_users_email", False),
    ],
)
def test_canonical_identifier_binding(
    validator: SQLValidator, sql: str, target: str, should_pass: bool
):
    """Postgres identifier canonicalization rules."""
    diff = Difference(
        kind="INDEX_MISSING",
        target=target,
        context={"table": "users", "columns": ["email"]},
    )
    change = SQLChange(statement=sql, operation="CREATE_INDEX", target=target)
    result = validator.validate(change, diff)
    if should_pass:
        assert result.status == "valid"
    else:
        assert result.status == "invalid"
        assert result.ghost_state == GhostState.BLOCKED


# --------------------------------------------------------------------------
# 7. Edge cases and injection attempts
# --------------------------------------------------------------------------


def test_sql_injection_attempt_blocked(
    validator: SQLValidator, index_missing_diff: Difference
):
    """SQL with injection attempt should be blocked by structural rules."""
    # This tries to sneak in a DROP via a comment + multi-statement
    change = SQLChange(
        statement="CREATE INDEX ix_users_email ON users (email); DROP TABLE users --",
        operation="CREATE_INDEX",
        target="ix_users_email",
    )
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "expected exactly 1 statement" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_empty_sql_rejected(validator: SQLValidator, index_missing_diff: Difference):
    """Empty SQL string."""
    change = SQLChange(statement="", operation="CREATE_INDEX", target="ix_users_email")
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "expected exactly 1 statement, got 0" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


def test_malformed_sql_rejected(
    validator: SQLValidator, index_missing_diff: Difference
):
    """Malformed SQL."""
    change = SQLChange(
        statement="CREAT INDEX ix ON users (email)",
        operation="CREATE_INDEX",
        target="ix",
    )  # typo
    result = validator.validate(change, index_missing_diff)
    assert result.status == "invalid"
    assert "SQL parse error" in result.reason
    assert result.ghost_state == GhostState.BLOCKED


# --------------------------------------------------------------------------
# 8. Integration with sqlglot AST assumptions (from earlier tests)
# --------------------------------------------------------------------------


def test_sqlglot_ast_assumptions_regression(validator: SQLValidator):
    """Verify sqlglot 30.29.0 AST assumptions still hold."""
    # Check that sqlglot still parses CREATE INDEX with WHERE as IndexParameters
    stmt = sqlglot.parse_one(
        "CREATE INDEX idx ON t (col) WHERE cond", dialect="postgres"
    )
    assert isinstance(stmt, exp.Create)
    assert isinstance(stmt.this, exp.Index)
    params = stmt.this.args.get("params")
    assert params is not None
    assert isinstance(params, exp.IndexParameters)
    assert params.args.get("where") is not None
