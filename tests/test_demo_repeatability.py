"""
test_demo_repeatability.py — Task 12.2, Req 39

Replays each app's demo twice and checks the second run ("the recording") is
identical to the first by signature and makes no live LLM or Context7 call.
Each app is wired the way its main.py wires it; only network I/O is faked.

  · App 1 Schema Drift — Hunt Ghosts through the real MantleResolver,
    comparator and validator, Exorcise every ghost, Reset Demo, then a fresh
    orchestrator on the same cache file (like an app restart). Every ghost must
    hit the cache with zero LLM and zero Context7 calls.
  · App 2 Contract Observer — Hunt Ghosts twice. The resolver makes no LLM call
    at scan time, so both runs must match with zero calls. Exorcise is NOT
    replayable: ContractExecutor asks the LLM for the patch and opens a GitHub
    PR on every apply, against the current file. That is a documented limit.
  · App 3 The Poltergeist — Hunt Ghosts through the real MockResolver,
    comparator and validator, Exorcise both ghosts, reset, replay. Every ghost
    hits the cache; there is never an LLM call.

The drift mirrors the demo: fixtures/seed.py for App 1, the demo target API
contract for App 2, the declared vs live Redis keys for App 3. No network, no
Neon, no MANTLE_API_KEY required.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adapters.openapi.comparator import ContractComparator
from adapters.openapi.validator import ContractValidator
from adapters.postgres.comparator import SchemaComparator
from adapters.postgres.validator import SQLValidator
from adapters.postgres.verifier import SchemaVerifier
from adapters.redis.comparator import CacheComparator
from adapters.redis.validator import CacheValidator
from agents import context7
from agents.resolver import MantleResolver, MockResolver
from core.cache import Cache
from core.models import (
    ComparisonTarget,
    ExecutionResult,
    GhostState,
    State,
    VerificationResult,
)
from core.orchestrator import Orchestrator
from core.persistence import Persistence

# ---------------------------------------------------------------------------
# Demo schemas — intended (models.py) vs actual (after fixtures/seed.py)
# ---------------------------------------------------------------------------


def _col(type_: str, *, nullable: bool = True, pk: bool = False) -> dict:
    return {"type": type_, "nullable": nullable, "primary_key": pk, "default": None}


INTENDED = {
    "users": {
        "columns": {
            "id": _col("integer", nullable=False, pk=True),
            "email": _col("varchar(255)", nullable=False),
            "age": _col("integer"),
        },
        "indexes": {},
        "primary_key": ["id"],
    },
    "orders": {
        "columns": {
            "id": _col("integer", nullable=False, pk=True),
            "user_id": _col("integer"),
            "status": _col("varchar(20)"),
        },
        "indexes": {"ix_orders_user_id": {"columns": ["user_id"], "unique": False}},
        "primary_key": ["id"],
    },
    "products": {
        "columns": {
            "id": _col("integer", nullable=False, pk=True),
            "name": _col("varchar(100)", nullable=False),
            "stock": _col("integer"),
        },
        "indexes": {},
        "primary_key": ["id"],
    },
}


def _seeded_actual() -> dict:
    """The drift fixtures/seed.py creates on Neon."""
    actual = copy.deepcopy(INTENDED)
    actual["users"]["columns"]["email"]["type"] = "varchar(50)"  # widening ghost
    actual["users"]["columns"]["age"]["type"] = "bigint"  # narrowing ghost
    actual["users"]["indexes"] = {"ix_users_email": {"columns": ["email"], "unique": False}}
    actual["orders"]["indexes"] = {}  # missing index ghost
    return actual


EXPECTED_DEMO = {
    ("INDEX_MISSING", "ix_orders_user_id"): GhostState.PENDING,
    ("TYPE_CHANGED", "users.email"): GhostState.PENDING,
    ("TYPE_CHANGED", "users.age"): GhostState.REVIEW_REQUIRED,
    ("INDEX_EXTRA", "ix_users_email"): GhostState.REVIEW_REQUIRED,
}


class _FakeDB:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tables = _seeded_actual()

    def state(self) -> State:
        return State(domain="database", data={"tables": copy.deepcopy(self.tables)})


class _Inputs:
    def targets(self):
        return [ComparisonTarget("database", "public")]


class _Source:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def intended(self, target=None) -> State:
        return State(domain="database", data={"tables": copy.deepcopy(INTENDED)})

    def actual(self, target=None) -> State:
        return self._db.state()


class _FakeExecutor:
    """Applies the validated fix to the in-memory DB so the real verifier sees it."""

    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def execute(self, resolution, difference):
        ctx = difference.context
        table = self._db.tables[ctx["table"]]
        if difference.kind == "INDEX_MISSING":
            table["indexes"][difference.target] = {"columns": list(ctx["columns"]), "unique": False}
        elif difference.kind == "TYPE_CHANGED":
            table["columns"][ctx["column"]]["type"] = ctx["intended_type"]
        elif difference.kind == "INDEX_EXTRA":
            table["indexes"].pop(difference.target)
        return ExecutionResult(
            difference_id=difference.id,
            success=True,
            applied_change=resolution.proposed_change,
        )


# ---------------------------------------------------------------------------
# Counting fakes for the LLM (ultra) and Context7
# ---------------------------------------------------------------------------


def _fake_sql(prompt: str) -> str:
    """What the LLM proposes for the ghost described in the prompt."""
    start = prompt.index("DIFFERENCE:\n") + len("DIFFERENCE:\n")
    diff, _ = json.JSONDecoder().raw_decode(prompt[start:])
    kind, target, ctx = diff["kind"], diff["target"], diff["context"]
    if kind == "INDEX_MISSING":
        sql, op = f"CREATE INDEX {target} ON {ctx['table']} ({', '.join(ctx['columns'])})", "CREATE_INDEX"
    elif kind == "TYPE_CHANGED":
        sql = f"ALTER TABLE {ctx['table']} ALTER COLUMN {ctx['column']} TYPE {ctx['intended_type']}"
        op = "ALTER_COLUMN_TYPE"
    elif kind == "INDEX_EXTRA":
        sql, op = f"DROP INDEX {target}", "DROP_INDEX"
    else:
        raise AssertionError(f"unexpected demo ghost {kind}")
    return json.dumps({"action": "sql", "statement": sql, "operation": op, "rationale": f"Fix {kind}."})


@pytest.fixture
def calls(monkeypatch):
    """Count LLM and Context7 calls; nothing leaves the process."""
    counts = {"llm": 0, "context7": 0}

    def fake_ultra(system, user, **kw):
        counts["llm"] += 1
        return _fake_sql(system)

    def fake_deepseek(system, user, **kw):  # App 2's patch model — must not run at scan time
        counts["llm"] += 1
        raise AssertionError("deepseek_pro called during a demo run")

    def fake_context7(url, headers):
        counts["context7"] += 1
        return 200, "### CREATE INDEX\nCREATE INDEX name ON table (column);"

    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.deepseek_pro", fake_deepseek)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    monkeypatch.setattr("agents.context7._http_get", fake_context7)
    monkeypatch.setattr("agents.context7.ssrf_validate", lambda url: url)
    context7.clear_cache()
    yield counts
    context7.clear_cache()


# ---------------------------------------------------------------------------
# Record → reset → replay
# ---------------------------------------------------------------------------


def _orchestrator(tmp_path: Path, db: _FakeDB) -> Orchestrator:
    source = _Source(db)
    return Orchestrator(
        input_source=_Inputs(),
        source_adapter=source,
        comparator=SchemaComparator(),
        resolver=MantleResolver(),
        executor=_FakeExecutor(db),
        verifier=SchemaVerifier(source.intended, source.actual),
        validator=SQLValidator(),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / ".cache.json"),
        domain="database",
        principal="demo-sandbox",
    )


def _hunt_and_exorcise(orch: Orchestrator):
    """Hunt Ghosts, then Exorcise every ghost the way the app's /apply does."""
    report = orch.run()
    before = {d.signature: d.state for d in report.differences}
    for diff in report.differences:
        orch.apply_one(diff.id, human_authorized=True)
    return report, before


def _outcomes(report, before) -> dict:
    """Semantic outcome per signature: everything except the fresh UUIDs."""
    out = {}
    for diff in report.differences:
        change = report.resolutions[diff.id].proposed_change
        out[diff.signature] = {
            "kind": diff.kind,
            "target": diff.target,
            "state_after_hunt": before[diff.signature],
            "operation": change.operation if change else None,
            "statement": change.payload.get("statement") if change else None,
            "final_state": diff.state,
        }
    return out


@pytest.fixture
def demo(tmp_path, calls):
    db = _FakeDB()

    record_report, record_before = _hunt_and_exorcise(_orchestrator(tmp_path, db))
    record_calls = dict(calls)

    db.reset()  # Reset Demo
    calls["llm"] = calls["context7"] = 0
    context7.clear_cache()  # any resolver call would have to reach the fake

    replay_report, replay_before = _hunt_and_exorcise(_orchestrator(tmp_path, db))  # app restart
    return {
        "record": record_report,
        "record_outcomes": _outcomes(record_report, record_before),
        "record_calls": record_calls,
        "replay": replay_report,
        "replay_outcomes": _outcomes(replay_report, replay_before),
        "replay_calls": dict(calls),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_run_matches_the_seeded_demo(demo):
    """The four ghosts judges see after Reset Demo, in the states the UI shows."""
    seen = {(o["kind"], o["target"]): o["state_after_hunt"] for o in demo["record_outcomes"].values()}
    assert seen == EXPECTED_DEMO


def test_first_run_calls_llm_and_context7(demo):
    """Sanity check: the fakes are wired in, so zero calls on replay means something."""
    assert demo["record_calls"]["llm"] == len(EXPECTED_DEMO)
    assert demo["record_calls"]["context7"] >= 1
    assert all(o["final_state"] == GhostState.RESOLVED for o in demo["record_outcomes"].values())


def test_recording_run_hits_cache_for_every_ghost(demo):
    resolutions = demo["replay"].resolutions.values()
    assert len(resolutions) == len(EXPECTED_DEMO)
    assert all(r.cached for r in resolutions), [r.difference_signature for r in resolutions if not r.cached]


def test_recording_run_makes_no_llm_or_context7_calls(demo):
    assert demo["replay_calls"] == {"llm": 0, "context7": 0}


def test_recording_run_has_identical_outcomes_by_signature(demo):
    """Same kind, target, starting state, operation, SQL and final state per signature."""
    assert demo["replay_outcomes"] == demo["record_outcomes"]


def test_recording_run_ghost_ids_are_fresh(demo):
    record_ids = {d.id for d in demo["record"].differences}
    replay_ids = {d.id for d in demo["replay"].differences}
    assert record_ids.isdisjoint(replay_ids)


# ===========================================================================
# App 2 — Contract Observer (Hunt Ghosts only; Exorcise is live by design)
# ===========================================================================

def _list_response(fields: list[str]) -> dict:
    props = {f: {"type": "string"} for f in fields}
    return {"200": {"schema": {"type": "array", "items": {"type": "object", "properties": props}}}}


API_INTENDED = State(domain="api", data={"paths": {
    "/users": {"get": {
        "operationId": "list_users",
        "schema_names": {"200": "User"},
        "responses": _list_response(["id", "email", "created_at"]),
    }},
}})
# Live API: drops created_at (FIELD_MISSING), adds account_status (FIELD_EXTRA).
API_ACTUAL = State(domain="api", data={"paths": {
    "/users": {"get": {"responses": _list_response(["id", "email", "account_status"])}},
}})


class _ApiSource:
    def intended(self, target):
        return API_INTENDED

    def actual(self, target):
        return API_ACTUAL


class _ApiInputs:
    def targets(self):
        return [ComparisonTarget("api", "customer-service", context={"repo": "demo/repo"})]


class _NoApply:
    """Contract Observer's executor opens real GitHub PRs; the scan never reaches it."""

    def execute(self, resolution, difference):
        raise AssertionError("executor called during a scan")


def _contract_orchestrator(tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        input_source=_ApiInputs(),
        source_adapter=_ApiSource(),
        comparator=ContractComparator(),
        resolver=MantleResolver(),
        executor=_NoApply(),
        verifier=MagicMock(),
        validator=ContractValidator(),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / ".cache.json"),
        domain="api",
        principal="demo-sandbox",
    )


def _scan_outcomes(report) -> dict:
    """Per signature: kind, target, state after Hunt Ghosts, and the proposal."""
    out = {}
    for diff in report.differences:
        change = report.resolutions[diff.id].proposed_change
        out[diff.signature] = {
            "kind": diff.kind,
            "target": diff.target,
            "state": diff.state,
            "operation": change.operation if change else None,
            "payload": change.payload if change else None,
        }
    return out


@pytest.fixture
def contract_demo(tmp_path, calls):
    first = _contract_orchestrator(tmp_path).run()
    first_calls = dict(calls)
    second = _contract_orchestrator(tmp_path).run()  # app restart, same cache file
    return {"first": first, "second": second, "first_calls": first_calls, "calls": dict(calls)}


def test_contract_demo_ghosts(contract_demo):
    kinds = sorted(d.kind for d in contract_demo["first"].differences)
    assert kinds == ["FIELD_EXTRA", "FIELD_MISSING"]


def test_contract_scan_is_identical_by_signature(contract_demo):
    assert _scan_outcomes(contract_demo["second"]) == _scan_outcomes(contract_demo["first"])


def test_contract_scan_makes_no_llm_or_context7_calls(contract_demo):
    """The patch LLM runs only inside ContractExecutor at Exorcise time."""
    assert contract_demo["first_calls"] == {"llm": 0, "context7": 0}
    assert contract_demo["calls"] == {"llm": 0, "context7": 0}


def test_contract_ghost_ids_are_fresh(contract_demo):
    first = {d.id for d in contract_demo["first"].differences}
    second = {d.id for d in contract_demo["second"].differences}
    assert first.isdisjoint(second)


# ===========================================================================
# App 3 — The Poltergeist (record → reset → replay)
# ===========================================================================

CACHE_DECLARED = {
    "users:list": {"description": "Cached list of all users"},
    "products:list": {"description": "Cached list of all products"},
}


class _FakeRedis:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        # products:list never warmed (KEY_MISSING); session:debug left behind (UNDECLARED_KEY)
        self.keys = ["users:list", "session:debug"]

    def state(self) -> State:
        return State(domain="cache", data={"keys": list(self.keys)})


class _CacheSource:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def intended(self, target=None):
        return State(domain="cache", data={"keys": copy.deepcopy(CACHE_DECLARED)})

    def actual(self, target=None):
        return self._redis.state()


class _FakeCacheExecutor:
    """Stands in for POST /cache/warm and DELETE /cache/delete on the demo API."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def execute(self, resolution, difference):
        if difference.kind == "KEY_MISSING":
            self._redis.keys.append(difference.target)
        elif difference.kind == "UNDECLARED_KEY":
            self._redis.keys.remove(difference.target)
        return ExecutionResult(
            difference_id=difference.id, success=True, applied_change=resolution.proposed_change
        )


class _FakeCacheVerifier:
    """Re-reads the fake Redis and re-runs the real comparator."""

    def __init__(self, source: _CacheSource) -> None:
        self._source = source

    def verify(self, target, difference):
        remaining = [
            d for d in CacheComparator().compare(self._source.intended(), self._source.actual())
            if d.kind == difference.kind and d.target == difference.target
        ]
        return VerificationResult(difference_id=difference.id, resolved=not remaining)


def _poltergeist_orchestrator(tmp_path: Path, redis: _FakeRedis) -> Orchestrator:
    source = _CacheSource(redis)
    return Orchestrator(
        input_source=MagicMock(targets=lambda: [ComparisonTarget("cache", "customer-service")]),
        source_adapter=source,
        comparator=CacheComparator(),
        resolver=MockResolver(),
        executor=_FakeCacheExecutor(redis),
        verifier=_FakeCacheVerifier(source),
        validator=CacheValidator(),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / ".cache.json"),
        domain="cache",
        principal="demo-sandbox",
    )


@pytest.fixture
def poltergeist_demo(tmp_path, calls):
    redis = _FakeRedis()
    record_orch = _poltergeist_orchestrator(tmp_path, redis)
    record = record_orch.run()
    record_scan = _scan_outcomes(record)
    for diff in record.differences:
        record_orch.apply_one(diff.id, human_authorized=True)

    redis.reset()  # Reset Demo
    replay_orch = _poltergeist_orchestrator(tmp_path, redis)  # app restart
    replay = replay_orch.run()
    replay_scan = _scan_outcomes(replay)
    for diff in replay.differences:
        replay_orch.apply_one(diff.id, human_authorized=True)
    return {
        "record": record, "record_scan": record_scan,
        "replay": replay, "replay_scan": replay_scan, "calls": dict(calls),
    }


def test_poltergeist_demo_ghosts(poltergeist_demo):
    seen = {(d.kind, d.target) for d in poltergeist_demo["record"].differences}
    assert seen == {("KEY_MISSING", "products:list"), ("UNDECLARED_KEY", "session:debug")}
    assert all(d.state == GhostState.RESOLVED for d in poltergeist_demo["record"].differences)


def test_poltergeist_recording_hits_cache_for_every_ghost(poltergeist_demo):
    resolutions = poltergeist_demo["replay"].resolutions.values()
    assert len(resolutions) == 2
    assert all(r.cached for r in resolutions)


def test_poltergeist_recording_is_identical_by_signature(poltergeist_demo):
    assert poltergeist_demo["replay_scan"] == poltergeist_demo["record_scan"]
    final = {d.signature: d.state for d in poltergeist_demo["replay"].differences}
    assert set(final.values()) == {GhostState.RESOLVED}


def test_poltergeist_never_calls_llm_or_context7(poltergeist_demo):
    assert poltergeist_demo["calls"] == {"llm": 0, "context7": 0}
