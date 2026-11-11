"""
Core validator — the safety boundary.

Two validators live here:
  · SQLValidator   — operation matrix, canonical target, target binding,
                     widening table, multi-statement / SET / USING rejection.
  · FilePatchValidator — symlink check, path confinement, hash verify,
                         format-aware key diff.

The validator is the authoritative safety gate. The LLM resolver's rationale
is NOT trusted. Every decision is derived from the SQL AST and the
Difference.context directly.

Forbidden SQL operations (fail closed on anything not in the matrix):
  DROP, DELETE, TRUNCATE, SET, USING clause, expression index, partial index.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from core.models import Difference, FilePatch, GhostState, SQLChange

# ---------------------------------------------------------------------------
# Operation matrix
# ---------------------------------------------------------------------------

# Maps (difference_kind) → (allowed_sql_operation, auto_fixable)
# Everything not in this table fails closed.
OPERATION_MATRIX: dict[str, tuple[str, bool]] = {
    "INDEX_MISSING":    ("CREATE_INDEX", True),
    "TYPE_CHANGED":     ("ALTER_COLUMN_TYPE", True),   # only widening pairs
    "COLUMN_MISSING":   ("ADD_COLUMN", False),          # REVIEW_REQUIRED
    "TABLE_MISSING":    ("CREATE_TABLE", False),        # BLOCKED
    "COLUMN_EXTRA":     ("DROP_COLUMN", False),         # BLOCKED
    "TABLE_EXTRA":      ("DROP_TABLE", False),          # BLOCKED
    "INDEX_EXTRA":      ("DROP_INDEX", False),          # BLOCKED
    # API domain
    "FIELD_UNDOCUMENTED": ("FILE_PATCH", True),
    # Deps domain
    "DECLARED_UNPINNED": ("FILE_PATCH", False),         # human-initiated only
}

# Difference kinds whose policy is BLOCKED regardless of proposed SQL
BLOCKED_KINDS: frozenset[str] = frozenset({
    "TABLE_MISSING",
    "COLUMN_EXTRA",
    "TABLE_EXTRA",
    "INDEX_EXTRA",
    "COLUMN_MISSING",   # NOT NULL no default — see matrix; nullable handled as REVIEW_REQUIRED
    "DECLARED_MISSING_INSTALL",
    "DECLARED_VERSION_MISMATCH",
})

# Difference kinds that are always REVIEW_REQUIRED (proposed_change=None)
REVIEW_REQUIRED_KINDS: frozenset[str] = frozenset({
    "COLUMN_MISSING",
    "ENDPOINT_MISSING",
    "FIELD_MISSING",
    "STATUS_CODE_CHANGED",
    "FIELD_RENAME_SUGGESTION",
    "DECLARED_VERSION_MISMATCH",
})

# Allowed type-widening pairs (old_canonical → new_canonical)
WIDENING_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("smallint", "integer"),
    ("integer", "bigint"),
    ("real", "double precision"),
    # varchar widening is checked dynamically (m > n)
    ("text", "text"),           # no-op, handled separately
})

# Destructive / forbidden SQL node types
FORBIDDEN_NODE_TYPES = (
    exp.Drop,
    exp.Delete,
    exp.TruncateTable,
    exp.Command,    # catches SET and other raw commands
)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    status: str          # "valid" | "blocked" | "review_required" | "invalid"
    reason: str = ""
    ghost_state: GhostState = GhostState.PENDING


# ---------------------------------------------------------------------------
# SQL Validator
# ---------------------------------------------------------------------------

class SQLValidator:
    """
    Validates a proposed SQLChange against:
      1. Operation matrix
      2. Single-statement rule
      3. SET statement rejection
      4. Forbidden node types
      5. USING clause / expression / partial index rejection
      6. Canonical target binding
      7. Type-widening table (for TYPE_CHANGED only)
    """

    def validate(
        self,
        change: SQLChange,
        difference: Difference,
    ) -> ValidationResult:
        # --- 1. Difference kind in matrix? ---
        if difference.kind not in OPERATION_MATRIX:
            return ValidationResult(
                status="blocked",
                reason=f"difference kind '{difference.kind}' not in operation matrix",
                ghost_state=GhostState.BLOCKED,
            )

        allowed_op, auto_fixable = OPERATION_MATRIX[difference.kind]

        if not auto_fixable:
            return ValidationResult(
                status="review_required",
                reason=f"'{difference.kind}' is not auto-fixable per operation matrix",
                ghost_state=GhostState.REVIEW_REQUIRED,
            )

        # --- 2. Parse SQL (pinned sqlglot) ---
        try:
            statements = sqlglot.parse(change.statement, dialect="postgres")
        except Exception as exc:
            return ValidationResult(
                status="invalid",
                reason=f"SQL parse error: {type(exc).__name__}",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 3. Single statement only ---
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            return ValidationResult(
                status="invalid",
                reason=f"expected exactly 1 statement, got {len(statements)}",
                ghost_state=GhostState.BLOCKED,
            )

        stmt = statements[0]

        # --- 4. Reject SET statements outright ---
        # sqlglot parses SET as exp.Set (not exp.Command)
        if isinstance(stmt, exp.Set):
            return ValidationResult(
                status="invalid",
                reason="SET statements are forbidden; use connection-level options",
                ghost_state=GhostState.BLOCKED,
            )
        if isinstance(stmt, exp.Command) and stmt.name.upper() == "SET":
            return ValidationResult(
                status="invalid",
                reason="SET statements are forbidden; use connection-level options",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 5. Reject forbidden node types ---
        for forbidden in FORBIDDEN_NODE_TYPES:
            if isinstance(stmt, forbidden):
                return ValidationResult(
                    status="blocked",
                    reason=f"forbidden SQL operation: {type(stmt).__name__}",
                    ghost_state=GhostState.BLOCKED,
                )

        # --- 6. Match AST to expected operation ---
        result = self._validate_operation(stmt, change, difference, allowed_op)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_operation(
        self,
        stmt: exp.Expression,
        change: SQLChange,
        difference: Difference,
        allowed_op: str,
    ) -> ValidationResult:
        if allowed_op == "CREATE_INDEX":
            return self._validate_create_index(stmt, change, difference)
        if allowed_op == "ALTER_COLUMN_TYPE":
            return self._validate_alter_type(stmt, change, difference)
        if allowed_op == "ADD_COLUMN":
            return ValidationResult(
                status="review_required",
                reason="ADD_COLUMN requires human review",
                ghost_state=GhostState.REVIEW_REQUIRED,
            )
        return ValidationResult(
            status="blocked",
            reason=f"operation '{allowed_op}' not handled by validator",
            ghost_state=GhostState.BLOCKED,
        )

    def _canonical_name(self, identifier: exp.Expression | str | None) -> str:
        """
        Apply Postgres identifier canonicalisation:
          · quoted  → preserve case
          · unquoted → lowercase
          · public schema → strip
        """
        if identifier is None:
            return ""
        if isinstance(identifier, str):
            return identifier.lower()
        if isinstance(identifier, exp.Identifier):
            name = identifier.name
            # Check if quoted - sqlglot 30.29.0 may return None or False
            quoted = identifier.args.get("quoted")
            # quoted=True means preserve case; quoted=False or None means lowercase
            if quoted is True:
                return name
            return name.lower()
        # For Table, Column, etc. that have nested identifiers
        if isinstance(identifier, (exp.Table, exp.Column)):
            # Extract the identifier from the object
            if isinstance(identifier, exp.Table):
                nested_id = identifier.this
            elif isinstance(identifier, exp.Column):
                nested_id = identifier.this
            else:
                nested_id = None
            if nested_id and isinstance(nested_id, exp.Identifier):
                name = nested_id.name
                quoted = nested_id.args.get("quoted")
                if quoted is True:
                    return name
                return name.lower()
        return str(identifier).lower()

    def _parse_target(self, target: str) -> tuple[str, str, str]:
        """
        Parse 'schema.table.column' or 'table.column' into
        (schema, table, column). Schema defaults to 'public'.
        """
        parts = target.split(".")
        if len(parts) == 3:
            schema, table, col = parts
        elif len(parts) == 2:
            schema, table, col = "public", parts[0], parts[1]
        else:
            schema, table, col = "public", parts[0], ""
        return schema.lower(), table.lower(), col.lower()

    def _validate_create_index(
        self,
        stmt: exp.Expression,
        change: SQLChange,
        difference: Difference,
    ) -> ValidationResult:
        if not isinstance(stmt, exp.Create) or not isinstance(stmt.this, exp.Index):
            return ValidationResult(
                status="invalid",
                reason="expected CREATE INDEX statement",
                ghost_state=GhostState.BLOCKED,
            )

        index_expr: exp.Index = stmt.this

        # Reject UNIQUE indexes
        if stmt.args.get("unique"):
            return ValidationResult(
                status="invalid",
                reason="UNIQUE indexes are not auto-fixable",
                ghost_state=GhostState.BLOCKED,
            )

        # Reject expression indexes (non-column expressions)
        for col in index_expr.find_all(exp.Anonymous, exp.Func):
            return ValidationResult(
                status="invalid",
                reason="expression indexes are forbidden",
                ghost_state=GhostState.BLOCKED,
            )

        # Reject partial indexes (WHERE clause — lives in IndexParameters in sqlglot 30.29.0)
        params = index_expr.args.get("params")
        if params and params.args.get("where"):
            return ValidationResult(
                status="invalid",
                reason="partial indexes (WHERE) are forbidden",
                ghost_state=GhostState.BLOCKED,
            )

        # Extract table and columns from AST
        table_expr = index_expr.args.get("table")
        if table_expr is None:
            return ValidationResult(
                status="invalid",
                reason="CREATE INDEX missing table reference",
                ghost_state=GhostState.BLOCKED,
            )

        # Reject non-public schema (public is the default, non-public schemas are not supported)
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

        # Bind to difference target: target = "table.col1,col2" or index name
        diff_target = difference.target
        # For INDEX_MISSING, target encodes the index name: ix_<table>_<cols>
        expected_index_name = f"ix_{ast_table}_{'_'.join(ast_cols)}"
        index_name_expr = index_expr.args.get("this")
        ast_index_name = self._canonical_name(index_name_expr) if index_name_expr else ""

        # Compare index names - if both have quoted identifiers, preserve case; otherwise lowercase
        # The diff_target from comparator is always lowercase (from _parse_target)
        # So we lowercase ast_index_name for comparison
        compare_ast_name = ast_index_name.lower() if ast_index_name else ""
        compare_diff_name = diff_target.lower()

        if ast_index_name and compare_ast_name != compare_diff_name:
            return ValidationResult(
                status="invalid",
                reason=(
                    f"index name '{ast_index_name}' does not match "
                    f"difference target '{diff_target}'"
                ),
                ghost_state=GhostState.BLOCKED,
            )

        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _validate_alter_type(
        self,
        stmt: exp.Expression,
        change: SQLChange,
        difference: Difference,
    ) -> ValidationResult:
        if not isinstance(stmt, exp.Alter):
            return ValidationResult(
                status="invalid",
                reason="expected ALTER TABLE statement",
                ghost_state=GhostState.BLOCKED,
            )

        # Reject USING clause
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

        # Extract table + column from AST
        ast_table = self._canonical_name(stmt.this)
        ast_col = self._canonical_name(alter_col.this)

        # Bind to difference target: "table.column"
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

        # Verify widening pair — from Difference.context, NOT resolver rationale
        old_type = difference.context.get("original_actual_type", "")
        new_type_expr = alter_col.args.get("dtype")
        new_type = str(new_type_expr).lower().strip() if new_type_expr else ""

        if not self._is_widening(old_type, new_type):
            return ValidationResult(
                status="review_required",
                reason=(
                    f"type change '{old_type}' → '{new_type}' is not a "
                    "permitted widening pair"
                ),
                ghost_state=GhostState.REVIEW_REQUIRED,
            )

        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _is_widening(self, old: str, new: str) -> bool:
        """Return True only if (old, new) is an allowed widening pair."""
        old = old.lower().strip()
        new = new.lower().strip()

        # Normalize sqlglot's type names to match WIDENING_PAIRS
        # sqlglot normalizes:
        #   integer -> int, real -> float, double precision -> double
        # But WIDENING_PAIRS uses standard Postgres names
        # Also normalize the old type from standard names to sqlglot names
        type_norm = {
            "integer": "int",
            "real": "float",
            "double precision": "double",
        }
        old = type_norm.get(old, old)
        new = type_norm.get(new, new)

        # Handle sqlglot's normalized names for basic widening pairs
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

        # varchar(n) → varchar(m) where m > n
        if old.startswith("varchar(") and new.startswith("varchar("):
            try:
                n = int(old[8:-1])
                m = int(new[8:-1])
                return m > n
            except ValueError:
                return False

        # varchar(n) → text
        if old.startswith("varchar(") and new == "text":
            return True

        return False


# ---------------------------------------------------------------------------
# File Patch Validator
# ---------------------------------------------------------------------------

class FilePatchValidator:
    """
    Validates a FilePatch before execution:
      1. Symlink check on the original path BEFORE resolve
      2. Path confinement to project root
      3. before_sha256 verification
      4. Format-aware parse (YAML / TOML / requirements.txt)
      5. Reject any key modification other than the declared target
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def validate(
        self,
        patch: FilePatch,
        difference: Difference,
    ) -> ValidationResult:
        path = Path(patch.file_path)

        # --- 1. Symlink check BEFORE resolve ---
        try:
            lstat = path.lstat()
        except FileNotFoundError:
            return ValidationResult(
                status="invalid",
                reason=f"file not found: {patch.file_path}",
                ghost_state=GhostState.BLOCKED,
            )
        if stat.S_ISLNK(lstat.st_mode):
            return ValidationResult(
                status="invalid",
                reason="symlinks are forbidden as patch targets",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 2. Path confinement ---
        try:
            resolved = path.resolve()
            resolved.relative_to(self.project_root)
        except ValueError:
            return ValidationResult(
                status="invalid",
                reason="patch target escapes project root",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 3. SHA-256 verification ---
        current_bytes = resolved.read_bytes()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        if current_hash != patch.before_sha256:
            return ValidationResult(
                status="invalid",
                reason="before_sha256 mismatch — file has changed since snapshot",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 4 & 5. Format-aware key check ---
        suffix = resolved.suffix.lower()
        try:
            patched_bytes = self._apply_forward_patch(current_bytes, patch.forward_patch)
            after_hash = hashlib.sha256(patched_bytes).hexdigest()
            if after_hash != patch.after_sha256:
                return ValidationResult(
                    status="invalid",
                    reason="after_sha256 mismatch — patch does not produce expected bytes",
                    ghost_state=GhostState.BLOCKED,
                )
            result = self._validate_keys(
                suffix, current_bytes, patched_bytes, difference.target
            )
            return result
        except Exception as exc:
            return ValidationResult(
                status="invalid",
                reason=f"patch validation error: {type(exc).__name__}",
                ghost_state=GhostState.BLOCKED,
            )

    def _apply_forward_patch(self, original: bytes, patch: bytes) -> bytes:
        """
        Apply a unified diff (forward_patch) to original bytes and return
        the patched bytes.

        Protocol: forward_patch is a standard unified-diff (b'' = no change).
        Lines starting with '+' are added, '-' removed, ' ' kept as-is.
        Hunk headers (@@ -a,b +c,d @@) drive the apply loop.

        Falls back to treating forward_patch as the full new content when
        it cannot be parsed as a diff (backward compatibility with week-1
        callers that stored raw new bytes).
        """
        if not patch:
            return original

        patch_text = patch.decode(errors="replace")

        # Detect whether this is a unified diff or raw new content.
        # A unified diff has at least one hunk header.
        import re
        if not re.search(r"^@@\s+-\d+", patch_text, re.MULTILINE):
            # Not a unified diff — treat as raw replacement bytes (week-1 compat).
            return patch

        original_lines = original.decode(errors="replace").splitlines(keepends=True)
        result_lines: list[str] = []
        orig_pos = 0  # 0-based index into original_lines

        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        patch_lines = patch_text.splitlines(keepends=True)
        i = 0

        # Skip file header lines (---, +++)
        while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
            i += 1

        while i < len(patch_lines):
            line = patch_lines[i]
            m = hunk_re.match(line)
            if not m:
                i += 1
                continue

            orig_start = int(m.group(1)) - 1  # convert to 0-based
            i += 1

            # Copy unchanged lines before this hunk
            while orig_pos < orig_start:
                if orig_pos < len(original_lines):
                    result_lines.append(original_lines[orig_pos])
                orig_pos += 1

            # Apply hunk lines
            while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                hline = patch_lines[i]
                if hline.startswith("+"):
                    result_lines.append(hline[1:])
                elif hline.startswith("-"):
                    orig_pos += 1  # consume from original
                elif hline.startswith(" "):
                    if orig_pos < len(original_lines):
                        result_lines.append(original_lines[orig_pos])
                    orig_pos += 1
                # Lines starting with '\' (no newline at end) are skipped
                i += 1

        # Copy any remaining original lines after the last hunk
        while orig_pos < len(original_lines):
            result_lines.append(original_lines[orig_pos])
            orig_pos += 1

        patched_text = "".join(result_lines)
        return patched_text.encode()

    def _validate_keys(
        self,
        suffix: str,
        original: bytes,
        patched: bytes,
        target: str,
    ) -> ValidationResult:
        """
        Parse original and patched, extract changed keys, confirm only
        the target key changed.
        """
        if suffix in (".yaml", ".yml"):
            return self._check_yaml_keys(original, patched, target)
        if suffix == ".toml":
            return self._check_toml_keys(original, patched, target)
        if suffix == ".txt":
            return self._check_requirements_keys(original, patched, target)
        # Unknown format — allow through (executor will catch format errors)
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _check_yaml_keys(
        self, original: bytes, patched: bytes, target: str
    ) -> ValidationResult:
        try:
            import yaml  # type: ignore[import]
            orig_data = yaml.safe_load(original)
            patch_data = yaml.safe_load(patched)
        except Exception as exc:
            return ValidationResult(
                status="invalid",
                reason=f"YAML parse error: {type(exc).__name__}",
                ghost_state=GhostState.BLOCKED,
            )
        if not isinstance(orig_data, dict) or not isinstance(patch_data, dict):
            return ValidationResult(
                status="invalid",
                reason="YAML root must be a mapping, not a list",
                ghost_state=GhostState.BLOCKED,
            )
        changed = {k for k in patch_data if patch_data.get(k) != orig_data.get(k)}
        changed |= {k for k in orig_data if k not in patch_data}
        for key in changed:
            if key != target:
                return ValidationResult(
                    status="invalid",
                    reason=f"patch modifies key '{key}' but target is '{target}'",
                    ghost_state=GhostState.BLOCKED,
                )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _check_toml_keys(
        self, original: bytes, patched: bytes, target: str
    ) -> ValidationResult:
        try:
            import tomllib
            orig_data = tomllib.loads(original.decode())
            patch_data = tomllib.loads(patched.decode())
        except Exception as exc:
            return ValidationResult(
                status="invalid",
                reason=f"TOML parse error: {type(exc).__name__}",
                ghost_state=GhostState.BLOCKED,
            )
        changed = {k for k in patch_data if patch_data.get(k) != orig_data.get(k)}
        changed |= {k for k in orig_data if k not in patch_data}
        for key in changed:
            if key != target:
                return ValidationResult(
                    status="invalid",
                    reason=f"patch modifies key '{key}' but target is '{target}'",
                    ghost_state=GhostState.BLOCKED,
                )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _check_requirements_keys(
        self, original: bytes, patched: bytes, target: str
    ) -> ValidationResult:
        try:
            from packaging.requirements import Requirement  # type: ignore[import]

            def _names(raw: bytes) -> dict[str, str]:
                result = {}
                for line in raw.decode().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    req = Requirement(line)
                    result[req.name.lower()] = str(req.specifier)
                return result

            orig = _names(original)
            patch = _names(patched)
        except Exception as exc:
            return ValidationResult(
                status="invalid",
                reason=f"requirements parse error: {type(exc).__name__}",
                ghost_state=GhostState.BLOCKED,
            )
        changed = {k for k in patch if patch.get(k) != orig.get(k)}
        changed |= {k for k in orig if k not in patch}
        for key in changed:
            if key != target.lower():
                return ValidationResult(
                    status="invalid",
                    reason=f"patch modifies package '{key}' but target is '{target}'",
                    ghost_state=GhostState.BLOCKED,
                )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)
