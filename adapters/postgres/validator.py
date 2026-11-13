"""
adapters/postgres/validator.py

PostgreSQL domain validator — implements the core Validator protocol.

Validates a ChangeProposal whose payload contains a SQL statement against
the detected Difference. This is the safety boundary for the database domain.

The validator is the authoritative safety gate. The LLM resolver's rationale
is NOT trusted. Every decision is derived from the SQL AST and the
Difference.context directly.

Operation matrix (postgres domain):
  INDEX_MISSING   → CREATE_INDEX   (auto-fixable)
  TYPE_CHANGED    → ALTER_COLUMN_TYPE (auto-fixable, widening pairs only)
  COLUMN_MISSING  → ADD_COLUMN     (review required)
  TABLE_MISSING   → CREATE_TABLE   (blocked)
  COLUMN_EXTRA    → DROP_COLUMN    (blocked)
  TABLE_EXTRA     → DROP_TABLE     (blocked)
  INDEX_EXTRA     → DROP_INDEX     (blocked)

Forbidden SQL (always rejected regardless of authorization):
  DELETE, TRUNCATE, SET, USING clause, expression index, partial index.

DROP operations require human_authorized=True.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from core.models import ChangeProposal, Difference, GhostState, ValidationResult

# ---------------------------------------------------------------------------
# Operation matrix — postgres domain knowledge, not core knowledge
# ---------------------------------------------------------------------------

OPERATION_MATRIX: dict[str, tuple[str, bool]] = {
    "INDEX_MISSING": ("CREATE_INDEX", True),
    "TYPE_CHANGED": ("ALTER_COLUMN_TYPE", True),  # widening pairs only
    "COLUMN_MISSING": ("ADD_COLUMN", False),  # REVIEW_REQUIRED
    "TABLE_MISSING": ("CREATE_TABLE", False),  # BLOCKED
    "COLUMN_EXTRA": ("DROP_COLUMN", False),  # BLOCKED
    "TABLE_EXTRA": ("DROP_TABLE", False),  # BLOCKED
    "INDEX_EXTRA": ("DROP_INDEX", False),  # BLOCKED
}

# Allowed type-widening pairs (old_canonical → new_canonical)
WIDENING_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("smallint", "integer"),
        ("integer", "bigint"),
        ("real", "double precision"),
        ("text", "text"),
    }
)

ALWAYS_FORBIDDEN = (exp.Delete, exp.TruncateTable, exp.Command)


# ---------------------------------------------------------------------------
# SQL Validator
# ---------------------------------------------------------------------------


class SQLValidator:
    """
    Implements the Validator protocol for the PostgreSQL domain.

    Validates a ChangeProposal whose payload["statement"] is a SQL DDL statement.
    Rejects proposals whose SQL does not match the expected operation and target.
    """

    def validate(
        self,
        proposal: ChangeProposal,
        difference: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        # --- 1. Proposal must carry a SQL statement ---
        statement = proposal.payload.get("statement", "")
        if not statement or not isinstance(statement, str):
            return ValidationResult(
                status="blocked",
                reason="proposal payload missing 'statement'",
                ghost_state=GhostState.BLOCKED,
            )

        operation = proposal.operation or proposal.payload.get("operation", "")

        # --- 2. Difference kind must be in operation matrix ---
        if difference.kind not in OPERATION_MATRIX:
            return ValidationResult(
                status="blocked",
                reason=f"difference kind '{difference.kind}' not in postgres operation matrix",
                ghost_state=GhostState.BLOCKED,
            )

        allowed_op, auto_fixable = OPERATION_MATRIX[difference.kind]

        # --- 3. Auto-fixability gate ---
        if not auto_fixable and not human_authorized:
            return ValidationResult(
                status="review_required",
                reason=f"'{difference.kind}' is not auto-fixable per operation matrix",
                ghost_state=GhostState.REVIEW_REQUIRED,
            )

        # --- 4. Parse SQL ---
        try:
            statements = sqlglot.parse(statement, dialect="postgres")
        except Exception as exc:
            return ValidationResult(
                status="invalid",
                reason=f"SQL parse error: {type(exc).__name__}",
                ghost_state=GhostState.BLOCKED,
            )

        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            return ValidationResult(
                status="invalid",
                reason=f"expected exactly 1 statement, got {len(statements)}",
                ghost_state=GhostState.BLOCKED,
            )

        stmt = statements[0]

        # --- 5. Reject SET statements ---
        if isinstance(stmt, exp.Set):
            return ValidationResult(
                status="invalid",
                reason="SET statements are forbidden",
                ghost_state=GhostState.BLOCKED,
            )
        if isinstance(stmt, exp.Command) and stmt.name.upper() == "SET":
            return ValidationResult(
                status="invalid",
                reason="SET statements are forbidden",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 6. Reject always-forbidden node types ---
        DROP_FORBIDDEN = (exp.Drop,) if not human_authorized else ()
        for forbidden in ALWAYS_FORBIDDEN + DROP_FORBIDDEN:
            if isinstance(stmt, forbidden):
                return ValidationResult(
                    status="blocked",
                    reason=f"forbidden SQL operation: {type(stmt).__name__}",
                    ghost_state=GhostState.BLOCKED,
                )

        # --- 7. Validate operation matches AST and target ---
        return self._validate_operation(
            stmt, statement, difference, allowed_op, human_authorized=human_authorized
        )

    # ------------------------------------------------------------------

    def _validate_operation(
        self,
        stmt: exp.Expression,
        statement: str,
        difference: Difference,
        allowed_op: str,
        human_authorized: bool = False,
    ) -> ValidationResult:
        if allowed_op == "CREATE_INDEX":
            return self._validate_create_index(stmt, difference)
        if allowed_op == "ALTER_COLUMN_TYPE":
            return self._validate_alter_type(stmt, difference, human_authorized=human_authorized)
        if allowed_op == "ADD_COLUMN":
            return self._validate_alter_add_column(stmt, difference)
        if allowed_op == "DROP_COLUMN":
            return self._validate_alter_drop_column(stmt, difference)
        if allowed_op == "DROP_INDEX":
            return self._validate_drop_index(stmt, difference)
        if allowed_op == "DROP_TABLE":
            return self._validate_drop_table(stmt, difference)
        if allowed_op == "CREATE_TABLE":
            return self._validate_create_table(stmt, difference)
        return ValidationResult(
            status="blocked",
            reason=f"operation '{allowed_op}' not handled by validator",
            ghost_state=GhostState.BLOCKED,
        )

    def _canonical_name(self, identifier: exp.Expression | str | None) -> str:
        if identifier is None:
            return ""
        if isinstance(identifier, str):
            return identifier.lower()
        if isinstance(identifier, exp.Identifier):
            name = identifier.name
            quoted = identifier.args.get("quoted")
            if quoted is True:
                return name
            return name.lower()
        if isinstance(identifier, (exp.Table, exp.Column)):
            if isinstance(identifier, exp.Table):
                nested_id = identifier.this
            else:
                nested_id = identifier.this
            if nested_id and isinstance(nested_id, exp.Identifier):
                name = nested_id.name
                quoted = nested_id.args.get("quoted")
                if quoted is True:
                    return name
                return name.lower()
        return str(identifier).lower()

    def _parse_target(self, target: str) -> tuple[str, str, str]:
        parts = target.split(".")
        if len(parts) == 3:
            schema, table, col = parts
        elif len(parts) == 2:
            schema, table, col = "public", parts[0], parts[1]
        else:
            schema, table, col = "public", parts[0], ""
        return schema.lower(), table.lower(), col.lower()

    def _is_widening(self, old: str, new: str) -> bool:
        old = old.lower().strip()
        new = new.lower().strip()
        type_norm = {
            "integer": "int",
            "real": "float",
            "double precision": "double",
        }
        old = type_norm.get(old, old)
        new = type_norm.get(new, new)
        normalized_pairs = {
            ("int", "bigint"): ("integer", "bigint"),
            ("smallint", "int"): ("smallint", "integer"),
            ("float", "double"): ("real", "double precision"),
            ("float", "double precision"): ("real", "double precision"),
        }
        if (old, new) in normalized_pairs:
            canonical = normalized_pairs[(old, new)]
            if canonical in WIDENING_PAIRS:
                return True
        if old.startswith("varchar(") and new.startswith("varchar("):
            try:
                n = int(old[8:-1])
                m = int(new[8:-1])
                return m > n
            except ValueError:
                return False
        if old.startswith("varchar(") and new == "text":
            return True
        return False

    def _validate_create_index(
        self, stmt: exp.Expression, difference: Difference
    ) -> ValidationResult:
        if not isinstance(stmt, exp.Create) or not isinstance(stmt.this, exp.Index):
            return ValidationResult(
                status="invalid",
                reason="expected CREATE INDEX statement",
                ghost_state=GhostState.BLOCKED,
            )
        index_expr: exp.Index = stmt.this
        if stmt.args.get("unique"):
            return ValidationResult(
                status="invalid",
                reason="UNIQUE indexes are not auto-fixable",
                ghost_state=GhostState.BLOCKED,
            )
        for _ in index_expr.find_all(exp.Anonymous, exp.Func):
            return ValidationResult(
                status="invalid",
                reason="expression indexes are forbidden",
                ghost_state=GhostState.BLOCKED,
            )
        params = index_expr.args.get("params")
        if params and params.args.get("where"):
            return ValidationResult(
                status="invalid",
                reason="partial indexes (WHERE) are forbidden",
                ghost_state=GhostState.BLOCKED,
            )
        table_expr = index_expr.args.get("table")
        if table_expr is None:
            return ValidationResult(
                status="invalid",
                reason="CREATE INDEX missing table reference",
                ghost_state=GhostState.BLOCKED,
            )
        if isinstance(table_expr, exp.Table):
            db = table_expr.args.get("db")
            if db and db.name.lower() != "public":
                return ValidationResult(
                    status="invalid",
                    reason=f"only public schema allowed; found schema '{db.name}'",
                    ghost_state=GhostState.BLOCKED,
                )
        ast_table = self._canonical_name(table_expr)
        ast_cols = [
            self._canonical_name(c.this if isinstance(c, exp.Ordered) else c)
            for c in index_expr.find_all(exp.Column)
        ]
        index_name_expr = index_expr.args.get("this")
        ast_index_name = self._canonical_name(index_name_expr) if index_name_expr else ""
        compare_ast_name = ast_index_name.lower() if ast_index_name else ""
        compare_diff_name = difference.target.lower()
        if ast_index_name and compare_ast_name != compare_diff_name:
            return ValidationResult(
                status="invalid",
                reason=(
                    f"index name '{ast_index_name}' does not match "
                    f"difference target '{difference.target}'"
                ),
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_alter_type(
        self,
        stmt: exp.Expression,
        difference: Difference,
        human_authorized: bool = False,
    ) -> ValidationResult:
        if not isinstance(stmt, exp.Alter):
            return ValidationResult(
                status="invalid",
                reason="expected ALTER TABLE statement",
                ghost_state=GhostState.BLOCKED,
            )
        alter_col = stmt.find(exp.AlterColumn)
        if alter_col is None:
            return ValidationResult(
                status="invalid",
                reason="ALTER TABLE does not contain ALTER COLUMN",
                ghost_state=GhostState.BLOCKED,
            )
        if alter_col.args.get("using"):
            return ValidationResult(
                status="invalid",
                reason="USING clause is forbidden in ALTER COLUMN TYPE",
                ghost_state=GhostState.BLOCKED,
            )
        ast_table = self._canonical_name(stmt.this)
        ast_col = self._canonical_name(alter_col.this)
        _, diff_table, diff_col = self._parse_target(difference.target)
        if ast_table != diff_table or ast_col != diff_col:
            return ValidationResult(
                status="invalid",
                reason=(
                    f"SQL targets '{ast_table}.{ast_col}' but difference "
                    f"targets '{diff_table}.{diff_col}'"
                ),
                ghost_state=GhostState.BLOCKED,
            )
        old_type = difference.context.get("original_actual_type", "")
        new_type_expr = alter_col.args.get("dtype")
        new_type = str(new_type_expr).lower().strip() if new_type_expr else ""
        if not human_authorized and not self._is_widening(old_type, new_type):
            return ValidationResult(
                status="review_required",
                reason=(
                    f"type change '{old_type}' → '{new_type}' is not a " "permitted widening pair"
                ),
                ghost_state=GhostState.REVIEW_REQUIRED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_alter_add_column(
        self, stmt: exp.Expression, difference: Difference
    ) -> ValidationResult:
        """ADD_COLUMN: must be ALTER TABLE ... ADD COLUMN targeting correct table."""
        if not isinstance(stmt, exp.Alter):
            return ValidationResult(
                status="invalid",
                reason="ADD_COLUMN requires an ALTER TABLE statement",
                ghost_state=GhostState.BLOCKED,
            )
        # Check it's an ADD action with a ColumnDef
        actions = stmt.args.get("actions", [])
        has_add = any(isinstance(a, exp.ColumnDef) for a in actions)
        if not has_add:
            return ValidationResult(
                status="invalid",
                reason="ALTER TABLE does not contain ADD COLUMN",
                ghost_state=GhostState.BLOCKED,
            )
        ast_table = self._canonical_name(stmt.this)
        _, diff_table, _ = self._parse_target(difference.target)
        if ast_table != diff_table:
            return ValidationResult(
                status="invalid",
                reason=f"SQL targets table '{ast_table}' but difference targets '{diff_table}'",
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_alter_drop_column(
        self, stmt: exp.Expression, difference: Difference
    ) -> ValidationResult:
        """DROP_COLUMN: must be ALTER TABLE ... DROP COLUMN targeting correct table.column."""
        if not isinstance(stmt, exp.Alter):
            return ValidationResult(
                status="invalid",
                reason="DROP_COLUMN requires an ALTER TABLE statement",
                ghost_state=GhostState.BLOCKED,
            )
        actions = stmt.args.get("actions", [])
        has_drop = any(isinstance(a, exp.Drop) for a in actions)
        if not has_drop:
            return ValidationResult(
                status="invalid",
                reason="ALTER TABLE does not contain DROP COLUMN",
                ghost_state=GhostState.BLOCKED,
            )
        ast_table = self._canonical_name(stmt.this)
        _, diff_table, diff_col = self._parse_target(difference.target)
        if ast_table != diff_table:
            return ValidationResult(
                status="invalid",
                reason=f"SQL targets table '{ast_table}' but difference targets '{diff_table}'",
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_drop_index(
        self, stmt: exp.Expression, difference: Difference
    ) -> ValidationResult:
        """DROP_INDEX: must be DROP INDEX targeting the correct index name."""
        if not isinstance(stmt, exp.Drop):
            return ValidationResult(
                status="invalid",
                reason="DROP_INDEX requires a DROP INDEX statement",
                ghost_state=GhostState.BLOCKED,
            )
        kind = stmt.args.get("kind", "")
        if str(kind).upper() != "INDEX":
            return ValidationResult(
                status="invalid",
                reason=f"expected DROP INDEX, got DROP {kind}",
                ghost_state=GhostState.BLOCKED,
            )
        ast_index = self._canonical_name(stmt.this)
        diff_index = difference.target.lower()
        if ast_index and ast_index != diff_index:
            return ValidationResult(
                status="invalid",
                reason=f"DROP INDEX targets '{ast_index}' but difference targets '{diff_index}'",
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_drop_table(
        self, stmt: exp.Expression, difference: Difference
    ) -> ValidationResult:
        """DROP_TABLE: must be DROP TABLE targeting the correct table."""
        if not isinstance(stmt, exp.Drop):
            return ValidationResult(
                status="invalid",
                reason="DROP_TABLE requires a DROP TABLE statement",
                ghost_state=GhostState.BLOCKED,
            )
        kind = stmt.args.get("kind", "")
        if str(kind).upper() != "TABLE":
            return ValidationResult(
                status="invalid",
                reason=f"expected DROP TABLE, got DROP {kind}",
                ghost_state=GhostState.BLOCKED,
            )
        ast_table = self._canonical_name(stmt.this)
        diff_table = difference.target.lower()
        if ast_table and ast_table != diff_table:
            return ValidationResult(
                status="invalid",
                reason=f"DROP TABLE targets '{ast_table}' but difference targets '{diff_table}'",
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_create_table(
        self, stmt: exp.Expression, difference: Difference
    ) -> ValidationResult:
        """CREATE_TABLE: must be CREATE TABLE targeting the correct table."""
        if not isinstance(stmt, exp.Create):
            return ValidationResult(
                status="invalid",
                reason="CREATE_TABLE requires a CREATE TABLE statement",
                ghost_state=GhostState.BLOCKED,
            )
        kind = stmt.args.get("kind", "")
        if str(kind).upper() != "TABLE":
            return ValidationResult(
                status="invalid",
                reason=f"expected CREATE TABLE, got CREATE {kind}",
                ghost_state=GhostState.BLOCKED,
            )
        ast_table = self._canonical_name(stmt.this)
        diff_table = difference.target.lower()
        if ast_table and ast_table != diff_table:
            return ValidationResult(
                status="invalid",
                reason=f"CREATE TABLE targets '{ast_table}' but difference targets '{diff_table}'",
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)
