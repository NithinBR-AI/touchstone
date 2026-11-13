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
from adapters.postgres.validator import SQLValidator
from adapters.postgres.verifier import SchemaVerifier
from agents.resolver import MockResolver, canned_responder
from core.cache import Cache
from core.models import ComparisonTarget, ExecutionResult, GhostState, State
from core.orchestrator import Orchestrator
from core.persistence import Persistence

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


class FakeDB:
    def __init__(self) -> None:
        self.tables = copy.deepcopy(INTENDED)
        self.tables["users"]["indexes"] = {}  # ghost: missing index
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

    def execute(self, resolution, difference=None):
        stmt = resolution.proposed_change.payload["statement"]
        if self.apply:
            if stmt.startswith("CREATE INDEX"):
                self.db.tables["users"]["indexes"]["ix_users_email"] = {
                    "columns": ["email"],
                    "unique": False,
                }
            elif "ALTER COLUMN email TYPE varchar(255)" in stmt:
                self.db.tables["users"]["columns"]["email"]["type"] = "varchar(255)"
        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=True,
            applied_change=resolution.proposed_change,
        )


def _orch(tmp_path: Path, db: FakeDB, resolver=None, apply=True) -> Orchestrator:
    source = Source(db)
    return Orchestrator(
        input_source=Inputs(),
        source_adapter=source,
        comparator=SchemaComparator(),
        resolver=resolver or MockResolver(),
        executor=FakeExecutor(db, apply=apply),
        verifier=SchemaVerifier(lambda: source.intended(None), db.state),
        validator=SQLValidator(),
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
    assert {d.id: d.state for d in saved.differences} == {d.id: d.state for d in report.differences}


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


def test_resolver_failure_requires_review_instead_of_pending(tmp_path: Path) -> None:
    orch = _orch(tmp_path, FakeDB(), resolver=MockResolver(lambda p, d: "garbage"))
    report = orch.run()
    assert report.differences
    # Resolver returns no proposal after repeated failures → REVIEW_REQUIRED
    assert all(d.state == GhostState.REVIEW_REQUIRED for d in report.differences)


def test_second_run_hits_cache(tmp_path: Path) -> None:
    """Cache is populated after apply+verify; second run hits cache for resolved diffs."""
    calls = []

    def counting(prompt, diff):
        calls.append(1)
        return canned_responder(prompt, diff)

    # First run + apply all diffs to populate the cache
    db1 = FakeDB()
    orch1 = _orch(tmp_path, db1, resolver=MockResolver(counting))
    report1 = orch1.run()
    for diff in report1.differences:
        orch1.apply_one(diff.id)
    first = len(calls)
    assert first >= 1

    # Second run — same diffs, same signatures → cache hit, responder not called again
    db2 = FakeDB()
    report2 = _orch(tmp_path, db2, resolver=MockResolver(counting)).run()
    assert len(calls) == first, "Responder called again on second run — cache miss"
    resolved_sigs = {
        r.difference_signature
        for r in report1.resolutions.values()
        if r.proposed_change is not None
    }
    for sig, res in report2.resolutions.items():
        diff = next(d for d in report2.differences if d.id == sig)
        if diff.signature in resolved_sigs:
            assert res.cached is True, f"Expected cache hit for {diff.signature}"


def test_cache_not_populated_before_apply(tmp_path: Path) -> None:
    """run() alone must NOT write to cache — cache only populated after verify."""
    db = FakeDB()
    _orch(tmp_path, db).run()
    cache_file = tmp_path / "cache.json"
    if cache_file.exists():
        data = cache_file.read_text(encoding="utf-8").strip()
        assert data in ("{}", ""), "Cache should be empty before any apply"


def test_cache_populated_after_successful_apply(tmp_path: Path) -> None:
    """Cache is written only after apply_one verifies the fix worked."""
    import json as _json

    db = FakeDB()
    orch = _orch(tmp_path, db)
    report = orch.run()
    pending = [d for d in report.differences if d.state == GhostState.PENDING]
    assert pending
    orch.apply_one(pending[0].id)
    cache_file = tmp_path / "cache.json"
    assert cache_file.exists()
    data = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert len(data) >= 1, "Expected at least one cache entry after successful apply"
    for entry in data.values():
        assert entry.get("proposed_change") is not None, "Cached entry has no proposed_change"
        assert "expires_at" in entry, "Cached entry missing expires_at TTL field"
