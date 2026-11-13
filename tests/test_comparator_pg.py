"""
test_comparator_pg.py — Task 2.10, Req 8

Uses in-memory State fixtures only. No database required.
"""

from __future__ import annotations

import copy

from adapters.postgres.comparator import SchemaComparator
from core.models import GhostState, State


def _col(type_: str, nullable: bool = True, default: str | None = None) -> dict:
    return {
        "type": type_,
        "nullable": nullable,
        "primary_key": False,
        "default": default,
    }


BASE = {
    "users": {
        "columns": {
            "id": _col("integer", nullable=False),
            "email": _col("varchar(100)", nullable=False),
            "bio": _col("text"),
        },
        "indexes": {"ix_users_email": {"columns": ["email"], "unique": False}},
        "primary_key": ["id"],
    }
}


def _state(tables: dict) -> State:
    return State(domain="database", data={"tables": copy.deepcopy(tables)})


def _by_kind(diffs, kind):
    return [d for d in diffs if d.kind == kind]


def test_control_has_no_false_positives() -> None:
    assert SchemaComparator().compare(_state(BASE), _state(BASE)) == []


def test_index_missing_is_auto_fixable_with_canonical_name() -> None:
    actual = copy.deepcopy(BASE)
    actual["users"]["indexes"] = {}
    diffs = SchemaComparator().compare(_state(BASE), _state(actual))
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == "INDEX_MISSING"
    assert d.target == "ix_users_email"
    assert d.context["safe_fix"] is True
    assert d.state == GhostState.PENDING


def test_widening_type_change_is_auto_fixable() -> None:
    intended = copy.deepcopy(BASE)
    intended["users"]["columns"]["email"] = _col("varchar(255)", nullable=False)
    diffs = SchemaComparator().compare(_state(intended), _state(BASE))
    d = _by_kind(diffs, "TYPE_CHANGED")[0]
    assert d.target == "users.email"
    assert d.context["original_actual_type"] == "varchar(100)"
    assert d.context["safe_fix"] is True


def test_non_widening_type_change_is_review_required() -> None:
    intended = copy.deepcopy(BASE)
    intended["users"]["columns"]["email"] = _col("varchar(50)", nullable=False)
    diffs = SchemaComparator().compare(_state(intended), _state(BASE))
    d = _by_kind(diffs, "TYPE_CHANGED")[0]
    assert d.context["safe_fix"] is False
    assert d.state == GhostState.REVIEW_REQUIRED


def test_column_missing_nullable_is_review_required() -> None:
    actual = copy.deepcopy(BASE)
    del actual["users"]["columns"]["bio"]
    d = SchemaComparator().compare(_state(BASE), _state(actual))[0]
    assert d.kind == "COLUMN_MISSING"
    assert d.state == GhostState.REVIEW_REQUIRED
    assert d.context["safe_fix"] is False


def test_column_missing_not_null_no_default_is_blocked() -> None:
    actual = copy.deepcopy(BASE)
    del actual["users"]["columns"]["email"]
    d = _by_kind(SchemaComparator().compare(_state(BASE), _state(actual)), "COLUMN_MISSING")[0]
    assert d.state == GhostState.BLOCKED


def test_destructive_kinds_are_blocked() -> None:
    actual = copy.deepcopy(BASE)
    actual["users"]["columns"]["legacy"] = _col("text")
    actual["users"]["indexes"]["ix_users_legacy"] = {
        "columns": ["legacy"],
        "unique": False,
    }
    actual["orphans"] = {
        "columns": {"id": _col("integer", False)},
        "indexes": {},
        "primary_key": ["id"],
    }
    diffs = SchemaComparator().compare(_state(BASE), _state(actual))
    kinds = {d.kind for d in diffs}
    assert {"COLUMN_EXTRA", "INDEX_EXTRA", "TABLE_EXTRA"} <= kinds
    assert all(d.state == GhostState.BLOCKED for d in diffs)
    assert all(d.context["safe_fix"] is False for d in diffs)


def test_table_missing_is_blocked() -> None:
    d = SchemaComparator().compare(_state(BASE), _state({}))[0]
    assert d.kind == "TABLE_MISSING"
    assert d.state == GhostState.BLOCKED


def test_unique_actual_indexes_are_not_reported_extra() -> None:
    actual = copy.deepcopy(BASE)
    actual["users"]["indexes"]["users_email_key"] = {
        "columns": ["email"],
        "unique": True,
    }
    actual["users"]["indexes"]["users_pkey"] = {"columns": ["id"], "unique": True}
    assert SchemaComparator().compare(_state(BASE), _state(actual)) == []


def test_signature_stable_across_runs_but_ids_differ() -> None:
    actual = copy.deepcopy(BASE)
    actual["users"]["indexes"] = {}
    a = SchemaComparator().compare(_state(BASE), _state(actual))[0]
    b = SchemaComparator().compare(_state(BASE), _state(actual))[0]
    assert a.signature == b.signature
    assert a.id != b.id
