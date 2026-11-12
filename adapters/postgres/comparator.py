"""
adapters/postgres/comparator.py — Req 8

Diffs intended vs actual schema State and emits Difference objects.

Per the SQL operation matrix (tech.md):

  INDEX_MISSING   plain non-unique index          -> safe_fix=True
  TYPE_CHANGED    (old, new) in widening table    -> safe_fix=True
  TYPE_CHANGED    any other pair                  -> safe_fix=False (REVIEW_REQUIRED)
  COLUMN_MISSING  nullable or has default         -> safe_fix=False (REVIEW_REQUIRED)
  COLUMN_MISSING  NOT NULL and no default         -> safe_fix=False (BLOCKED)
  TABLE_MISSING / COLUMN_EXTRA / TABLE_EXTRA / INDEX_EXTRA -> BLOCKED

The comparator only classifies. The core validator independently enforces
the matrix; nothing here is trusted as a safety decision.

Difference.target formats:
  TABLE_*          "<table>"
  COLUMN_*/TYPE_*  "<table>.<column>"
  INDEX_MISSING    "ix_<table>_<col1>_<col2>"  (canonical name from the matrix)
  INDEX_EXTRA      "<actual index name>"
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.models import Difference, GhostState, State
from core.validator import SQLValidator

DOMAIN = "database"

# Policy values stored in Difference.context["policy"]
POLICY_AUTO = "auto_fixable"
POLICY_REVIEW = "review_required"
POLICY_BLOCKED = "blocked"

_WIDENING = SQLValidator()


def _signature(kind: str, target: str, key_context: dict[str, Any]) -> str:
    """Stable hash: same inputs -> same signature across runs (cache key)."""
    payload = json.dumps(
        {"domain": DOMAIN, "kind": kind, "target": target, "ctx": key_context},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _make(
    kind: str,
    target: str,
    severity: str,
    policy: str,
    context: dict[str, Any],
    key_context: dict[str, Any] | None = None,
) -> Difference:
    ctx = dict(context)
    ctx["policy"] = policy
    ctx["safe_fix"] = policy == POLICY_AUTO
    state = {
        POLICY_AUTO: GhostState.PENDING,
        POLICY_REVIEW: GhostState.REVIEW_REQUIRED,
        POLICY_BLOCKED: GhostState.BLOCKED,
    }[policy]
    return Difference(
        signature=_signature(kind, target, key_context or {}),
        domain=DOMAIN,
        kind=kind,
        target=target,
        severity=severity,
        state=state,
        context=ctx,
    )


def _index_name(table: str, columns: list[str]) -> str:
    return f"ix_{table}_{'_'.join(columns)}"


class SchemaComparator:
    """Comparator protocol implementation for the Postgres schema domain."""

    def compare(self, intended: State, actual: State) -> list[Difference]:
        i_tables: dict[str, Any] = intended.data.get("tables", {})
        a_tables: dict[str, Any] = actual.data.get("tables", {})
        diffs: list[Difference] = []

        for table in sorted(i_tables):
            if table not in a_tables:
                diffs.append(
                    _make(
                        "TABLE_MISSING", table, "HIGH", POLICY_BLOCKED, {"table": table}
                    )
                )
                continue
            diffs.extend(self._compare_columns(table, i_tables[table], a_tables[table]))
            diffs.extend(self._compare_indexes(table, i_tables[table], a_tables[table]))

        for table in sorted(a_tables):
            if table not in i_tables:
                diffs.append(
                    _make(
                        "TABLE_EXTRA", table, "MEDIUM", POLICY_BLOCKED, {"table": table}
                    )
                )

        return diffs

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------

    def _compare_columns(
        self, table: str, i_tbl: dict[str, Any], a_tbl: dict[str, Any]
    ) -> list[Difference]:
        diffs: list[Difference] = []
        i_cols: dict[str, Any] = i_tbl["columns"]
        a_cols: dict[str, Any] = a_tbl["columns"]

        for col in sorted(i_cols):
            target = f"{table}.{col}"
            spec = i_cols[col]
            if col not in a_cols:
                has_default = spec.get("default") is not None
                nullable = bool(spec.get("nullable", True))
                policy = POLICY_REVIEW if (nullable or has_default) else POLICY_BLOCKED
                diffs.append(
                    _make(
                        "COLUMN_MISSING",
                        target,
                        "HIGH",
                        policy,
                        {
                            "table": table,
                            "column": col,
                            "intended_type": spec["type"],
                            "nullable": nullable,
                            "has_default": has_default,
                        },
                        {"type": spec["type"]},
                    )
                )
                continue

            old_type = a_cols[col]["type"]
            new_type = spec["type"]
            if old_type != new_type:
                unsupported = old_type.startswith(
                    "UNSUPPORTED:"
                ) or new_type.startswith("UNSUPPORTED:")
                widening = (not unsupported) and _WIDENING._is_widening(
                    old_type, new_type
                )
                diffs.append(
                    _make(
                        "TYPE_CHANGED",
                        target,
                        "MEDIUM",
                        POLICY_AUTO if widening else POLICY_REVIEW,
                        {
                            "table": table,
                            "column": col,
                            "original_actual_type": old_type,
                            "intended_type": new_type,
                        },
                        {"old": old_type, "new": new_type},
                    )
                )

        for col in sorted(a_cols):
            if col not in i_cols:
                diffs.append(
                    _make(
                        "COLUMN_EXTRA",
                        f"{table}.{col}",
                        "MEDIUM",
                        POLICY_BLOCKED,
                        {
                            "table": table,
                            "column": col,
                            "actual_type": a_cols[col]["type"],
                        },
                        {"type": a_cols[col]["type"]},
                    )
                )

        return diffs

    # ------------------------------------------------------------------
    # Indexes (matched by column list, not by name)
    # ------------------------------------------------------------------

    def _compare_indexes(
        self, table: str, i_tbl: dict[str, Any], a_tbl: dict[str, Any]
    ) -> list[Difference]:
        diffs: list[Difference] = []
        i_idx: dict[str, Any] = i_tbl.get("indexes", {})
        a_idx: dict[str, Any] = a_tbl.get("indexes", {})

        actual_by_cols = {tuple(v["columns"]): (k, v) for k, v in a_idx.items()}
        intended_cols = {tuple(v["columns"]) for v in i_idx.values()}

        for name in sorted(i_idx):
            spec = i_idx[name]
            cols = list(spec["columns"])
            if tuple(cols) in actual_by_cols:
                continue
            unique = bool(spec.get("unique"))
            diffs.append(
                _make(
                    "INDEX_MISSING",
                    _index_name(table, cols),
                    "LOW",
                    POLICY_REVIEW if unique else POLICY_AUTO,
                    {
                        "table": table,
                        "columns": cols,
                        "unique": unique,
                        "declared_name": name,
                    },
                    {"columns": cols, "unique": unique},
                )
            )

        # Only non-unique actual indexes can be "extra". Unique indexes back
        # constraints (PK/UNIQUE) that SQLAlchemy does not list as Index objects.
        for name in sorted(a_idx):
            spec = a_idx[name]
            if spec.get("unique"):
                continue
            if tuple(spec["columns"]) in intended_cols:
                continue
            diffs.append(
                _make(
                    "INDEX_EXTRA",
                    name,
                    "LOW",
                    POLICY_BLOCKED,
                    {"table": table, "columns": list(spec["columns"])},
                    {"columns": list(spec["columns"])},
                )
            )

        return diffs
