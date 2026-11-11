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

from core.cache import Cache, CACHE_VERSION, VALIDATOR_CONTRACT
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
            "id":    {"type": "integer",      "nullable": False, "primary_key": True,  "default": None},
            "email": {"type": "varchar(255)", "nullable": False, "primary_key": False, "default": None},
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
    def execute(self, resolution):
        stmt = resolution.proposed_change.statement
        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=True,
            applied_change=resolution.proposed_change,
        )


# ---------------------------------------------------------------------------
# Orchestrator factory
# ---------------------------------------------------------------------------

def _build_orch(tmp_path: Path, db: _FakeDB, responder=None) -> Orchestrator:
    from agents.resolver import MockResolver, canned_responder
    from adapters.postgres.comparator import SchemaComparator
    from adapters.postgres.verifier import SchemaVerifier

    resolver = MockResolver(responder or canned_responder)
    source = _Source(db)

    return Orchestrator(
        input_source=_Inputs(),
        source_adapter=source,
        comparator=SchemaComparator(),
        resolver=resolver,
        executor=_FakeExecutor(),
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

class TestCacheReproducibility:

    def test_second_run_hits_cache_responder_not_called(self, tmp_path: Path) -> None:
        """Responder is called once; second orchestrator run reads from cache."""
        calls: list[int] = []

        from agents.resolver import canned_responder

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        db1 = _FakeDB()
        _build_orch(tmp_path, db1, counting).run()
        first_count = len(calls)

        db2 = _FakeDB()
        _build_orch(tmp_path, db2, counting).run()

        assert first_count >= 1, "Responder was never called on first run"
        assert len(calls) == first_count, (
            f"Responder called again on second run: {len(calls)} total vs {first_count} after first"
        )

    def test_second_run_resolutions_marked_cached(self, tmp_path: Path) -> None:
        """All resolutions on the second run have cached=True."""
        db1 = _FakeDB()
        _build_orch(tmp_path, db1).run()

        db2 = _FakeDB()
        report2 = _build_orch(tmp_path, db2).run()

        assert report2.resolutions, "No resolutions produced"
        for sig, res in report2.resolutions.items():
            assert res.cached is True, f"Resolution {sig} not marked cached"

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
        """Cache file entries carry cache_version, validator_contract, prompt_hash."""
        db = _FakeDB()
        _build_orch(tmp_path, db).run()

        cache_file = tmp_path / "cache.json"
        assert cache_file.exists(), "Cache file not written"

        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data, "Cache file is empty"

        for sig, entry in data.items():
            assert "cache_version" in entry, f"cache_version missing for {sig}"
            assert "validator_contract" in entry, f"validator_contract missing for {sig}"
            assert "prompt_hash" in entry, f"prompt_hash missing for {sig}"
            assert entry["cache_version"] == CACHE_VERSION
            assert entry["validator_contract"] == VALIDATOR_CONTRACT

    def test_cache_lookup_skips_on_version_mismatch(self, tmp_path: Path) -> None:
        """Cache.get returns None when cache_version in file differs."""
        db = _FakeDB()
        report1 = _build_orch(tmp_path, db).run()

        # Corrupt the cache_version on all entries
        cache_file = tmp_path / "cache.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        for sig in data:
            data[sig]["cache_version"] = "0.0.0-stale"
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        # Second run with same inputs: cache should be skipped → responder called again
        calls: list[int] = []
        from agents.resolver import canned_responder

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        db2 = _FakeDB()
        _build_orch(tmp_path, db2, counting).run()
        assert len(calls) >= 1, "Expected responder to be called after version mismatch"

    def test_cache_lookup_skips_on_prompt_hash_mismatch(self, tmp_path: Path) -> None:
        """Cache.get returns None when prompt_hash in file differs."""
        db = _FakeDB()
        _build_orch(tmp_path, db).run()

        # Corrupt the prompt_hash on all entries
        cache_file = tmp_path / "cache.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        for sig in data:
            data[sig]["prompt_hash"] = "deadbeef" * 8  # wrong 64-char hex
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        calls: list[int] = []
        from agents.resolver import canned_responder

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
