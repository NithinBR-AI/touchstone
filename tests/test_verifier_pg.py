"""
test_verifier_pg.py — Task 2.11, Req 23

Uses injected state loaders. No database required.
"""

from __future__ import annotations

import copy

from adapters.postgres.comparator import SchemaComparator
from adapters.postgres.verifier import SchemaVerifier
from core.models import ComparisonTarget, State

INTENDED = {
    "users": {
        "columns": {
            "id": {
                "type": "integer",
                "nullable": False,
                "primary_key": True,
                "default": None,
            },
            "email": {
                "type": "varchar(255)",
                "nullable": False,
                "primary_key": False,
                "default": None,
            },
        },
        "indexes": {"ix_users_email": {"columns": ["email"], "unique": False}},
        "primary_key": ["id"],
    }
}


def _state(tables: dict) -> State:
    return State(domain="database", data={"tables": copy.deepcopy(tables)})


def _drifted() -> dict:
    t = copy.deepcopy(INTENDED)
    t["users"]["indexes"] = {}
    return t


def _index_diff():
    diffs = SchemaComparator().compare(_state(INTENDED), _state(_drifted()))
    return next(d for d in diffs if d.kind == "INDEX_MISSING")


def test_resolved_when_ghost_gone() -> None:
    diff = _index_diff()
    v = SchemaVerifier(lambda: _state(INTENDED), lambda: _state(INTENDED))
    result = v.verify(ComparisonTarget("database", diff.target), diff)
    assert result.resolved is True
    assert result.remaining is None
    assert result.difference_id == diff.id
    # Evidence carries the re-read table, not just a boolean
    assert result.evidence["table"] == "users"
    assert "email" in result.evidence["actual_table"]["columns"]
    assert result.evidence["actual_table"]["indexes"]


def test_still_present_when_ghost_remains() -> None:
    diff = _index_diff()
    v = SchemaVerifier(lambda: _state(INTENDED), lambda: _state(_drifted()))
    result = v.verify(ComparisonTarget("database", diff.target), diff)
    assert result.resolved is False
    assert result.remaining is not None
    assert result.remaining.kind == "INDEX_MISSING"
    assert result.remaining.target == diff.target


def test_unrelated_differences_do_not_block_resolution() -> None:
    diff = _index_diff()
    other_drift = copy.deepcopy(INTENDED)
    other_drift["users"]["columns"]["email"][
        "type"
    ] = "varchar(100)"  # a different ghost
    v = SchemaVerifier(lambda: _state(INTENDED), lambda: _state(other_drift))
    assert v.verify(ComparisonTarget("database", diff.target), diff).resolved is True
