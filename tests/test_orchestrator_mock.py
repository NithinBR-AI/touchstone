"""
test_orchestrator_mock.py — full loop with the mock resolver, no database.

compare -> resolve (mock) -> validate -> apply (fake executor mutates an
in-memory "database") -> verify (re-reads the in-memory state).

Task 2.19 repeats this against real Postgres once a DB is reachable.
"""

from __future__ import annotations

import copy
from pathlib import Path

from adapters.postgres.comparator import SchemaComparator
from adapters.postgres.verifier import SchemaVerifier
from agents.resolver import MockResolver
from core.cache import Cache
from core.models import ComparisonTarget, ExecutionResult, GhostState, State
from core.orchestrator import Orchestrator
from core.persistence import Persistence
from core.validator import FilePatchValidator, SQLValidator

INTENDED = {
    "users": {
        "columns": {
            "id": {"type": "integer", "nullable": False, "primary_key": True, "default": None},
            "email": {"type": "varchar(255)", "nullable": False, "primary_key": False, "default": None},
        },
        "indexes": {"ix_users_email": {"columns": ["email"], "unique": False}},
        "primary_key": ["id"],
    }
}


class FakeDB:
    def __init__(self) -> None:
        self.tables = copy.deepcopy(INTENDED)
        self.tables["users"]["indexes"] = {}                      # ghost: missing index
        self.tables["users"]["columns"]["email"]["type"] = "varchar(100)"  # ghost: narrower type

    def state(self) -> State:
        return State(domain="database", data={"tables": copy.deepcopy(self.tables)})


class Inputs:
    def targets(self):
        return [ComparisonTarget("database", "public")]


class Source:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def intended(self, target):
        return State(domain="database", data={"tables": copy.deepcopy(INTENDED)})

    def actual(self, target):
        return self.db.state()


class FakeExecutor:
    """Applies the two statements the mock resolver emits to the in-memory DB."""

    def __init__(self, db: FakeDB, apply: bool = True) -> None:
        self.db, self.apply = db, apply

    def execute(self, resolution):
        stmt = resolution.proposed_change.statement
        if self.apply:
            if stmt.startswith("CREATE INDEX"):
                self.db.tables["users"]["indexes"]["ix_users_email"] = {
                    "columns": ["email"], "unique": False}
            elif "ALTER COLUMN email TYPE varchar(255)" in stmt:
                self.db.tables["users"]["columns"]["email"]["type"] = "varchar(255)"
        return ExecutionResult(difference_id=resolution.difference_id, success=True,
                               applied_change=resolution.proposed_change)


def _orch(tmp_path: Path, db: FakeDB, resolver=None, apply=True) -> Orchestrator:
    source = Source(db)
    return Orchestrator(
        input_source=Inputs(),
        source_adapter=source,
        comparator=SchemaComparator(),
        resolver=resolver or MockResolver(),
        executor=FakeExecutor(db, apply=apply),
        verifier=SchemaVerifier(lambda: source.intended(None), db.state),
        validator_sql=SQLValidator(),
        validator_file=FilePatchValidator(tmp_path),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / "cache.json"),
        domain="database",
    )


def test_full_loop_resolves_both_ghosts(tmp_path: Path) -> None:
    db = FakeDB()
    orch = _orch(tmp_path, db)
    report = orch.run()

    kinds = {d.kind: d for d in report.differences}
    assert set(kinds) == {"INDEX_MISSING", "TYPE_CHANGED"}
    assert all(d.state == GhostState.PENDING for d in report.differences)

    for diff in report.differences:
        outcome = orch.apply_one(diff.id)
        assert outcome.state == GhostState.RESOLVED
        assert outcome.verification.resolved
        assert report.outcomes[diff.id] is outcome  # outcome is recorded

    # Persisted report matches in-memory state
    saved = Persistence(tmp_path / "reports").load(report.id)
    assert {d.id: d.state for d in saved.differences} == {
        d.id: d.state for d in report.differences}


def test_apply_without_effect_is_still_present(tmp_path: Path) -> None:
    db = FakeDB()
    orch = _orch(tmp_path, db, apply=False)
    report = orch.run()
    diff = report.differences[0]
    outcome = orch.apply_one(diff.id)
    assert outcome.state == GhostState.STILL_PRESENT
    assert outcome.verification.remaining is not None


def test_second_apply_is_refused(tmp_path: Path) -> None:
    db = FakeDB()
    orch = _orch(tmp_path, db)
    diff = orch.run().differences[0]
    orch.apply_one(diff.id)
    assert orch.apply_one(diff.id).state == GhostState.RESOLVED
    assert orch.apply_one(diff.id).execution is None  # refused, not re-executed


def test_resolver_failure_blocks_instead_of_pending(tmp_path: Path) -> None:
    orch = _orch(tmp_path, FakeDB(), resolver=MockResolver(lambda p, d: "garbage"))
    report = orch.run()
    assert report.differences
    assert all(d.state == GhostState.BLOCKED for d in report.differences)


def test_second_run_hits_cache(tmp_path: Path) -> None:
    calls = []

    from agents.resolver import canned_responder

    def counting(prompt, diff):
        calls.append(1)
        return canned_responder(prompt, diff)

    _orch(tmp_path, FakeDB(), resolver=MockResolver(counting)).run()
    first = len(calls)
    report = _orch(tmp_path, FakeDB(), resolver=MockResolver(counting)).run()
    assert first == 2
    assert len(calls) == first  # no new responder calls
    assert all(r.cached for r in report.resolutions.values())
