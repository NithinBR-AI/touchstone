"""Rollback tests - file success, external edit, double rollback, DB refusal."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.cache import Cache
from core.models import (ComparisonTarget, Difference, ExecutionResult,
                         FilePatch, GhostState, Resolution, RollbackEvent,
                         SQLChange, State, VerificationResult)
from core.orchestrator import Orchestrator
from core.persistence import Persistence
from core.validator import FilePatchValidator, SQLValidator, ValidationResult


def create_orchestrator(domain: str = "api"):
    """Create orchestrator with mocked adapters."""
    input_source = MagicMock()
    input_source.targets.return_value = [
        ComparisonTarget(domain=domain, target="file.yml", context={})
    ]

    source_adapter = MagicMock()
    source_adapter.intended.return_value = State(domain=domain, data={"key": "value"})
    source_adapter.actual.return_value = State(domain=domain, data={"key": "old"})

    diff = Difference(
        kind="FIELD_UNDOCUMENTED",
        target="file.yml",
        domain=domain,
        id="test-diff",
        context={
            "original_actual": "old",
            "original_intended": "value",
        },
    )
    diff.signature = "FIELD_UNDOCUMENTED:file.yml:old"

    comparator = MagicMock()
    comparator.compare.return_value = [diff]

    resolver = MagicMock()
    _patch = FilePatch(
        file_path="file.yml",
        before_sha256="abc123",
        after_sha256="def456",
        forward_patch=b"key: value",
        reverse_patch=b"key: old",
    )

    resolver.resolve.return_value = Resolution(
        difference_id=diff.id,
        difference_signature=diff.signature,
        proposed_change=_patch,
        rationale="Test",
        prompt_sha256="abc123",
    )

    executor = MagicMock()
    executor.execute.return_value = ExecutionResult(
        difference_id=diff.id,
        success=True,
        applied_change=_patch,
    )
    executor.rollback.return_value = True

    verifier = MagicMock()
    verifier.verify.return_value = VerificationResult(
        difference_id=diff.id,
        resolved=True,
        evidence={"key": "value"},
    )

    validator_sql = SQLValidator()
    # Mock the file validator so it always passes — rollback tests are testing
    # orchestrator rollback behaviour, not the file patch validator itself.
    validator_file = MagicMock(spec=FilePatchValidator)
    validator_file.validate.return_value = ValidationResult(status="valid")

    persistence = MagicMock(spec=Persistence)
    cache = MagicMock(spec=Cache)
    cache.get.return_value = None

    return Orchestrator(
        input_source=input_source,
        source_adapter=source_adapter,
        comparator=comparator,
        resolver=resolver,
        executor=executor,
        verifier=verifier,
        validator_sql=validator_sql,
        validator_file=validator_file,
        persistence=persistence,
        cache=cache,
        domain=domain,
    )


class TestRollbackDBRefusal:
    """Database domain rollback is refused."""

    def test_db_rollback_refused(self):
        """DB rollback always refused with reason."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        assert "rollback not supported" in event.reason

    def test_db_rollback_state_unchanged(self):
        """DB rollback - state unchanged (always REFUSED)."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        orch.apply_one(report.differences[0].id)

        original_state = report.differences[0].state
        orch.rollback(report.differences[0].id)

        assert report.differences[0].state == original_state
        assert report.differences[0].state == GhostState.RESOLVED


class TestRollbackFileSuccess:
    """File domain rollback succeeds."""

    def test_file_rollback_resolved(self):
        """File rollback RESOLVED -> success, state unchanged."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)
        assert event.success is True
        assert report.differences[0].state == GhostState.RESOLVED

    def test_file_rollback_still_present(self):
        """File rollback STILL_PRESENT -> success, state unchanged."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff",
            resolved=False,
            evidence={"key": "old"},
        )
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)
        assert event.success is True
        assert report.differences[0].state == GhostState.STILL_PRESENT

    def test_file_rollback_state_unchanged(self):
        """File rollback - state unchanged."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.apply_one(report.differences[0].id)

        original_state = report.differences[0].state
        orch.rollback(report.differences[0].id)

        assert report.differences[0].state == original_state


class TestRollbackExternalEdit:
    """Rollback fails when file was externally edited."""

    def test_rollback_external_edit(self):
        """Rollback fails when executor.rollback returns False."""
        orch = create_orchestrator(domain="api")
        orch.executor.rollback.return_value = False

        report = orch.run()
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        assert "executor reported rollback failure" in event.reason

    def test_rollback_executor_exception(self):
        """Rollback fails when executor raises."""
        orch = create_orchestrator(domain="api")
        orch.executor.rollback.side_effect = Exception("Disk error")

        report = orch.run()
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        # Orchestrator stores type(exc).__name__ — not the exception message
        assert event.reason != ""


class TestRollbackDouble:
    """Double rollback - refused (no execution record)."""

    def test_double_rollback_refused(self):
        """Second rollback is refused when executor signals failure on second call."""
        orch = create_orchestrator(domain="api")
        # First call succeeds, second call fails (simulates file already restored)
        orch.executor.rollback.side_effect = [True, False]

        report = orch.run()
        orch.apply_one(report.differences[0].id)

        # First rollback succeeds
        event1 = orch.rollback(report.differences[0].id)
        assert event1.success is True

        # Second rollback refused — executor signals failure
        event2 = orch.rollback(report.differences[0].id)
        assert event2.success is False


class TestRollbackEvents:
    """Rollback events are properly recorded."""

    def test_rollback_event_recorded(self):
        """Rollback event recorded in report.rollback_events."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)

        assert report.differences[0].id in report.rollback_events
        assert event in report.rollback_events[report.differences[0].id]

    def test_rollback_event_fields(self):
        """RollbackEvent has correct fields."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.apply_one(report.differences[0].id)

        event = orch.rollback(report.differences[0].id)

        assert isinstance(event, RollbackEvent)
        assert event.difference_id == "test-diff"
        assert hasattr(event, "attempted_at")
        assert hasattr(event, "success")
        assert hasattr(event, "reason")
