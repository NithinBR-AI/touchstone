"""
adapters/postgres/verifier.py — Req 23

Re-reads both sides after an apply and re-runs the comparison. The ghost is
resolved only if no difference with the same kind and target remains.

VerificationResult.evidence carries the exact re-read table state, not just a
boolean (product.md principle 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from adapters.postgres.actual import load_actual_schema
from adapters.postgres.comparator import SchemaComparator
from adapters.postgres.intended import load_intended_schema
from core.models import ComparisonTarget, Difference, State, VerificationResult

StateLoader = Callable[[], State]


class SchemaVerifier:
    """Verifier protocol implementation for the database domain."""

    def __init__(
        self,
        load_intended: StateLoader,
        load_actual: StateLoader,
        comparator: SchemaComparator | None = None,
    ) -> None:
        self._load_intended = load_intended
        self._load_actual = load_actual
        self._comparator = comparator or SchemaComparator()

    @classmethod
    def for_database(cls, models_path: Path, dsn: str, schema: str = "public") -> "SchemaVerifier":
        return cls(
            load_intended=lambda: load_intended_schema(models_path),
            load_actual=lambda: load_actual_schema(dsn, schema),
        )

    def verify(self, target: ComparisonTarget, difference: Difference) -> VerificationResult:
        intended = self._load_intended()
        actual = self._load_actual()
        remaining = next(
            (
                d
                for d in self._comparator.compare(intended, actual)
                if d.kind == difference.kind and d.target == difference.target
            ),
            None,
        )

        table = difference.context.get("table") or difference.target.split(".")[0]
        evidence = {
            "checked_kind": difference.kind,
            "checked_target": difference.target,
            "table": table,
            "actual_captured_at": actual.captured_at.isoformat(),
            "actual_table": actual.data.get("tables", {}).get(table),
        }

        return VerificationResult(
            difference_id=difference.id,
            resolved=remaining is None,
            evidence=evidence,
            remaining=remaining,
        )
