"""
test_postgres_e2e.py — Task 2.19, Req 15, 19, 20, 23

End-to-end: compare → resolve (mock) → validate → execute → verify against
real Postgres seeded from fixtures/seed.py.

Run after seeding:
    docker compose -f fixtures/docker-compose.yml up -d
    python fixtures/seed.py postgresql://touchstone:touchstone@localhost:5432/touchstone_demo
    DATABASE_URL=postgresql://touchstone:touchstone@localhost:5432/touchstone_demo pytest tests/test_postgres_e2e.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DB_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="DATABASE_URL not set — skipping real Postgres e2e tests",
)

DEMO_MODELS = Path(__file__).parent.parent / "demo" / "models.py"


def _build_orch(tmp_path: Path):
    from adapters.postgres.actual import load_actual_schema
    from adapters.postgres.comparator import SchemaComparator
    from adapters.postgres.executor import SQLExecutor
    from adapters.postgres.intended import load_intended_schema
    from adapters.postgres.verifier import SchemaVerifier
    from agents.resolver import MockResolver
    from core.cache import Cache
    from core.models import ComparisonTarget, State
    from core.orchestrator import Orchestrator
    from core.persistence import Persistence
    from core.validator import FilePatchValidator, SQLValidator

    class _InputSource:
        def targets(self):
            return [ComparisonTarget("database", "public")]

    class _SourceAdapter:
        def intended(self, target):
            return load_intended_schema(DEMO_MODELS)

        def actual(self, target):
            return load_actual_schema(DB_URL)

    return Orchestrator(
        input_source=_InputSource(),
        source_adapter=_SourceAdapter(),
        comparator=SchemaComparator(),
        resolver=MockResolver(),
        executor=SQLExecutor(DB_URL),
        verifier=SchemaVerifier(
            load_intended=lambda: load_intended_schema(DEMO_MODELS),
            load_actual=lambda: load_actual_schema(DB_URL),
        ),
        validator_sql=SQLValidator(),
        validator_file=FilePatchValidator(tmp_path),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / "cache.json"),
        domain="database",
    )


class TestPostgresE2E:

    def test_comparator_finds_expected_ghosts(self, tmp_path):
        """Seeded DB produces at least INDEX_MISSING and TYPE_CHANGED ghosts."""
        orch = _build_orch(tmp_path)
        report = orch.run()
        kinds = {d.kind for d in report.differences}
        assert "INDEX_MISSING" in kinds, f"Expected INDEX_MISSING in {kinds}"
        assert "TYPE_CHANGED" in kinds, f"Expected TYPE_CHANGED in {kinds}"

    def test_mock_resolver_proposes_valid_sql(self, tmp_path):
        """Mock resolver proposes SQL that passes the validator for auto-fixable ghosts."""
        from core.models import GhostState

        orch = _build_orch(tmp_path)
        report = orch.run()

        auto_fixable = [
            d for d in report.differences if d.state == GhostState.PENDING
        ]
        assert auto_fixable, "Expected at least one PENDING (auto-fixable) difference"

        for diff in auto_fixable:
            res = report.resolutions[diff.id]
            assert res.proposed_change is not None, (
                f"{diff.kind} has no proposed_change after resolve+validate"
            )

    def test_apply_one_resolves_index_missing(self, tmp_path):
        """Applying INDEX_MISSING difference reaches RESOLVED with evidence."""
        from core.models import GhostState

        orch = _build_orch(tmp_path)
        report = orch.run()

        index_diff = next(
            (d for d in report.differences if d.kind == "INDEX_MISSING" and d.state == GhostState.PENDING),
            None,
        )
        assert index_diff is not None, "No PENDING INDEX_MISSING difference found"

        outcome = orch.apply_one(index_diff.id)
        assert outcome.state == GhostState.RESOLVED, (
            f"Expected RESOLVED, got {outcome.state}. "
            f"execution={outcome.execution}, verification={outcome.verification}"
        )
        assert outcome.verification is not None and outcome.verification.resolved

    def test_apply_one_resolves_type_changed(self, tmp_path):
        """Applying TYPE_CHANGED widening reaches RESOLVED."""
        from core.models import GhostState

        orch = _build_orch(tmp_path)
        report = orch.run()

        type_diff = next(
            (d for d in report.differences if d.kind == "TYPE_CHANGED" and d.state == GhostState.PENDING),
            None,
        )
        assert type_diff is not None, "No PENDING TYPE_CHANGED difference found"

        outcome = orch.apply_one(type_diff.id)
        assert outcome.state == GhostState.RESOLVED, (
            f"Expected RESOLVED, got {outcome.state}. execution={outcome.execution}"
        )

    def test_second_run_hits_cache(self, tmp_path):
        """Second orchestrator run reads resolutions from cache; responder not called again."""
        from agents.resolver import MockResolver, canned_responder

        # Check there are PENDING diffs to resolve — if DB was mutated by an
        # earlier test, skip rather than asserting a stale state.
        from core.models import GhostState
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as _td:
            _probe = _build_orch(pathlib.Path(_td))
            _probe_report = _probe.run()
        pending = [d for d in _probe_report.differences if d.state == GhostState.PENDING]
        if not pending:
            import pytest
            pytest.skip("No PENDING differences in DB — drift may have been applied by an earlier test")

        calls: list[int] = []

        def counting(prompt, diff):
            calls.append(1)
            return canned_responder(prompt, diff)

        orch1 = _build_orch(tmp_path)
        orch1.resolver = MockResolver(counting)
        orch1.run()
        first = len(calls)

        orch2 = _build_orch(tmp_path)
        orch2.resolver = MockResolver(counting)
        orch2.run()

        assert first >= 1, "Resolver was never called on first run — no PENDING diffs resolved"
        assert len(calls) == first, "Responder called again on second run — cache miss"
