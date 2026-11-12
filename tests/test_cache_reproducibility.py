"""
test_cache_reproducibility.py — Task 1.0e, Req 15 and 33

Cache keyed by (signature, cache_version, validator_contract, prompt_hash).
  · Two runs with same inputs: second run hits cache (responder not called again).
  · Resolutions from second run have cached=True.
  · Semantic outcomes match by signature across runs (same operation/statement).
  · Difference UUIDs differ between runs (fresh id on each Difference instantiation).
  · Cache entries carry cache_version, validator_contract, prompt_hash.
  · Lookup skips on any key mismatch (version, contract, hash).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.resolver import MockResolver, canned_responder
from core.cache import CACHE_VERSION, VALIDATOR_CONTRACT, Cache
from core.models import (
    ComparisonTarget,
    Difference,
    ExecutionResult,
    GhostState,
    Resolution,
    SQLChange,
    State,
    VerificationResult,
)
from core.orchestrator import Orchestrator
from core.persistence import Persistence
from core.validator import FilePatchValidator, SQLValidator

# ---------------------------------------------------------------------------
# In-memory fake DB (same pattern as test_orchestrator_mock.py)
# ---------------------------------------------------------------------------

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


class _FakeDB:
    def __init__(self) -> None:
        self.tables = copy.deepcopy(INTENDED)
        self.tables["users"]["indexes"] = {}  # ghost: missing index

    def state(self):
        return State(domain="database", data={"tables": copy.deepcopy(self.tables)})


class _Source:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def intended(self, target):
        return State(domain="database", data={"tables": copy.deepcopy(INTENDED)})

    def actual(self, target):
        return self._db.state()


class _Inputs:
    def targets(self):
        return [ComparisonTarget("database", "public")]


class _FakeExecutor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def execute(self, resolution):
        stmt = resolution.proposed_change.statement
        # Actually mutate the in-memory DB so the verifier sees the fix
        if stmt.startswith("CREATE INDEX"):
            self._db.tables["users"]["indexes"]["ix_users_email"] = {
                "columns": ["email"], "unique": False
            }
        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=True,
            applied_change=resolution.proposed_change,
        )


# ---------------------------------------------------------------------------
# Orchestrator factory
# ---------------------------------------------------------------------------


def _build_orch(tmp_path: Path, db: _FakeDB, responder=None) -> Orchestrator:
    from adapters.postgres.comparator import SchemaComparator
    from adapters.postgres.verifier import SchemaVerifier

    resolver = MockResolver(responder or canned_responder)
    source = _Source(db)

    return Orchestrator(
        input_source=_Inputs(),
        source_adapter=source,
        comparator=SchemaComparator(),
        resolver=resolver,
        executor=_FakeExecutor(db),
        verifier=SchemaVerifier(lambda: source.intended(None), db.state),
        validator_sql=SQLValidator(),
        validator_file=FilePatchValidator(tmp_path),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / "cache.json"),
        domain="database",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run_and_apply_all(tmp_path: Path, db: _FakeDB, responder=None) -> None:
    """Helper: run + apply all PENDING diffs to populate the cache."""
    orch = _build_orch(tmp_path, db, responder)
    report = orch.run()
    for diff in report.differences:
        if diff.state == GhostState.PENDING:
            orch.apply_one(diff.id)


class TestCacheReproducibility:

    def test_second_run_hits_cache_responder_not_called(self, tmp_path: Path) -> None:
        """Responder called on first run+apply; second run reads from cache."""
        calls: list[int] = []

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        db1 = _FakeDB()
        _run_and_apply_all(tmp_path, db1, counting)
        first_count = len(calls)

        db2 = _FakeDB()
        _build_orch(tmp_path, db2, counting).run()

        assert first_count >= 1, "Responder was never called on first run"
        assert (
            len(calls) == first_count
        ), f"Responder called again on second run: {len(calls)} total vs {first_count} after first"

    def test_second_run_resolutions_marked_cached(self, tmp_path: Path) -> None:
        """Resolutions for applied+verified diffs are marked cached=True on second run."""
        db1 = _FakeDB()
        _run_and_apply_all(tmp_path, db1)

        db2 = _FakeDB()
        report2 = _build_orch(tmp_path, db2).run()

        assert report2.resolutions, "No resolutions produced"
        cached = [r for r in report2.resolutions.values() if r.cached]
        assert cached, "No resolutions marked cached on second run after apply"

    def test_semantic_outcomes_match_by_signature(self, tmp_path: Path) -> None:
        """Proposed operation and statement are identical across both runs (same signature)."""
        db1 = _FakeDB()
        report1 = _build_orch(tmp_path, db1).run()

        db2 = _FakeDB()
        report2 = _build_orch(tmp_path, db2).run()

        # Build signature → proposed_change map for each run
        def by_sig(report):
            return {
                res.difference_signature: res.proposed_change
                for res in report.resolutions.values()
                if res.proposed_change is not None
            }

        changes1 = by_sig(report1)
        changes2 = by_sig(report2)

        assert changes1.keys() == changes2.keys(), "Signatures differ between runs"
        for sig in changes1:
            c1, c2 = changes1[sig], changes2[sig]
            assert type(c1) == type(c2), f"Change type mismatch for {sig}"
            if isinstance(c1, SQLChange):
                assert c1.operation == c2.operation, f"Operation mismatch for {sig}"
                assert c1.statement == c2.statement, f"Statement mismatch for {sig}"

    def test_difference_uuids_differ_between_runs(self, tmp_path: Path) -> None:
        """Each run produces fresh Difference UUIDs (not reused from cache)."""
        db1 = _FakeDB()
        report1 = _build_orch(tmp_path, db1).run()

        db2 = _FakeDB()
        report2 = _build_orch(tmp_path, db2).run()

        ids1 = {d.id for d in report1.differences}
        ids2 = {d.id for d in report2.differences}
        assert ids1.isdisjoint(ids2), f"UUIDs overlapped between runs: {ids1 & ids2}"

    def test_cache_entries_carry_provenance_keys(self, tmp_path: Path) -> None:
        """Cache file entries carry cache_version, validator_contract, prompt_hash, expires_at."""
        db = _FakeDB()
        _run_and_apply_all(tmp_path, db)

        cache_file = tmp_path / "cache.json"
        assert cache_file.exists(), "Cache file not written after apply"

        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data, "Cache file is empty after apply"

        for sig, entry in data.items():
            assert "cache_version" in entry, f"cache_version missing for {sig}"
            assert "validator_contract" in entry, f"validator_contract missing for {sig}"
            assert "prompt_hash" in entry, f"prompt_hash missing for {sig}"
            assert "expires_at" in entry, f"expires_at TTL missing for {sig}"
            assert entry["cache_version"] == CACHE_VERSION
            assert entry["validator_contract"] == VALIDATOR_CONTRACT
            assert entry["proposed_change"] is not None, f"null proposed_change cached for {sig}"

    def test_cache_lookup_skips_on_version_mismatch(self, tmp_path: Path) -> None:
        """Cache.get returns None when cache_version in file differs."""
        db = _FakeDB()
        _run_and_apply_all(tmp_path, db)

        # Corrupt the cache_version on all entries
        cache_file = tmp_path / "cache.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        for sig in data:
            data[sig]["cache_version"] = "0.0.0-stale"
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        calls: list[int] = []

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        db2 = _FakeDB()
        _build_orch(tmp_path, db2, counting).run()
        assert len(calls) >= 1, "Expected responder to be called after version mismatch"

    def test_cache_lookup_skips_on_prompt_hash_mismatch(self, tmp_path: Path) -> None:
        """Cache.get returns None when prompt_hash in file differs."""
        db = _FakeDB()
        _run_and_apply_all(tmp_path, db)

        # Corrupt the prompt_hash on all entries
        cache_file = tmp_path / "cache.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        for sig in data:
            data[sig]["prompt_hash"] = "deadbeef" * 8
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        calls: list[int] = []

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        db2 = _FakeDB()
        _build_orch(tmp_path, db2, counting).run()
        assert len(calls) >= 1, "Expected responder to be called after prompt_hash mismatch"

    def test_first_run_no_cached_flag(self, tmp_path: Path) -> None:
        """First run resolutions are NOT marked cached (they came from the resolver)."""
        db = _FakeDB()
        report = _build_orch(tmp_path, db).run()

        for sig, res in report.resolutions.items():
            assert res.cached is False, f"Resolution {sig} wrongly marked cached on first run"

    def test_cache_not_written_before_apply(self, tmp_path: Path) -> None:
        """run() alone must not write to cache — only apply+verify does."""
        db = _FakeDB()
        _build_orch(tmp_path, db).run()
        cache_file = tmp_path / "cache.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            assert data == {}, "Cache should be empty before any apply"

    def test_cache_ttl_expiry_forces_fresh_call(self, tmp_path: Path) -> None:
        """Expired cache entries are evicted and the responder is called again."""
        from datetime import datetime, timedelta, timezone as tz
        db = _FakeDB()
        _run_and_apply_all(tmp_path, db)

        # Backdate expires_at to force expiry
        cache_file = tmp_path / "cache.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        past = (datetime.now(tz.utc) - timedelta(days=1)).isoformat()
        for sig in data:
            data[sig]["expires_at"] = past
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        calls: list[int] = []

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        db2 = _FakeDB()
        _build_orch(tmp_path, db2, counting).run()
        assert len(calls) >= 1, "Expected responder called after TTL expiry"

    def test_null_proposed_change_never_cached(self, tmp_path: Path) -> None:
        """Review-only resolutions (proposed_change=None) must never appear in cache."""
        import json as _json
        # Use a responder that always returns review-only
        db = _FakeDB()
        _run_and_apply_all(tmp_path, db, responder=lambda p, d: {
            "action": "review", "statement": None, "operation": None, "rationale": "manual"
        })
        cache_file = tmp_path / "cache.json"
        if cache_file.exists():
            data = _json.loads(cache_file.read_text(encoding="utf-8"))
            for sig, entry in data.items():
                assert entry.get("proposed_change") is not None, \
                    f"Null proposed_change was cached for {sig}"
