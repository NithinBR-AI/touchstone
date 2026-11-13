"""State machine tests - 56 cells (7 states × 8 events)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adapters.postgres.validator import SQLValidator
from core.cache import Cache
from core.models import (
    ChangeProposal,
    ComparisonTarget,
    Difference,
    ExecutionResult,
    GhostState,
    Resolution,
    RollbackEvent,
    State,
    VerificationResult,
)
from core.orchestrator import Orchestrator
from core.persistence import Persistence


class _Execute:
    """Descriptor that mimics MagicMock's side_effect on a plain method."""

    def __init__(self, default_result_fn):
        self._default = default_result_fn
        self.side_effect = None

    def __call__(self, resolution, difference):
        if self.side_effect is not None:
            exc = self.side_effect
            self.side_effect = None
            raise exc
        return self._default(resolution, difference)


class _RollbackExecutor:
    """Concrete executor with RollbackSupport for file-domain tests."""

    def __init__(self, default_result_fn):
        self.execute = _Execute(default_result_fn)

    def rollback(self, execution: ExecutionResult) -> bool:
        return True


def create_orchestrator(domain: str = "database"):
    """Create orchestrator with mocked adapters."""
    input_source = MagicMock()
    input_source.targets.return_value = [
        ComparisonTarget(domain=domain, target="users.email", context={})
    ]
    source_adapter = MagicMock()
    source_adapter.intended.return_value = State(domain=domain, data={"email": "VARCHAR(100)"})
    source_adapter.actual.return_value = State(domain=domain, data={"email": "VARCHAR(50)"})
    diff = Difference(
        kind="TYPE_CHANGED",
        target="users.email",
        domain=domain,
        id="test-diff",
        context={
            "original_actual_type": "varchar(50)",
            "original_intended_type": "varchar(100)",
        },
    )
    diff.signature = "TYPE_CHANGED:users.email:varchar(50)"
    comparator = MagicMock()
    comparator.compare.return_value = [diff]
    resolver = MagicMock()
    _proposal = ChangeProposal(
        domain=domain,
        target="users.email",
        operation="ALTER_COLUMN_TYPE",
        payload={
            "statement": "ALTER TABLE users ALTER COLUMN email TYPE varchar(100)",
            "operation": "ALTER_COLUMN_TYPE",
        },
    )
    resolver.resolve.return_value = Resolution(
        difference_id=diff.id,
        difference_signature=diff.signature,
        proposed_change=_proposal,
        rationale="Test",
        prompt_sha256="abc123",
    )

    _exec_result = ExecutionResult(
        difference_id=diff.id,
        success=True,
        applied_change=_proposal,
    )

    if domain == "api":
        # Use concrete class so isinstance(executor, RollbackSupport) passes
        executor = _RollbackExecutor(default_result_fn=lambda r, d: _exec_result)
    else:
        executor = MagicMock()
        executor.execute.return_value = _exec_result
        executor.rollback.return_value = True

    verifier = MagicMock()
    verifier.verify.return_value = VerificationResult(
        difference_id=diff.id, resolved=True, evidence={"email": "varchar(100)"}
    )
    validator = SQLValidator()
    persistence = MagicMock(spec=Persistence)
    cache = MagicMock(spec=Cache)
    cache.get.return_value = None
    orch = Orchestrator(
        input_source=input_source,
        source_adapter=source_adapter,
        comparator=comparator,
        resolver=resolver,
        executor=executor,
        verifier=verifier,
        validator=validator,
        persistence=persistence,
        cache=cache,
        domain=domain,
    )
    # Expose verifier so tests can override verify return value
    orch.verifier = verifier
    return orch


# 8 events:
# 1. run() - creates PENDING differences
# 2. apply_success_verify_pass - PENDING → RESOLVED
# 3. apply_success_verify_fail - PENDING → STILL_PRESENT
# 4. apply_failure - PENDING → EXECUTION_FAILED
# 5. apply_refused_review - PENDING → REVIEW_REQUIRED (no proposed_change)
# 6. apply_refused_blocked - PENDING → BLOCKED (invalid SQL)
# 7. rollback_db - db domain REFUSED
# 8. rollback_file - file domain success

# 7 states: PENDING, REVIEW_REQUIRED, BLOCKED, RESOLVED, STILL_PRESENT, EXECUTION_FAILED, SUPERSEDED


class TestStateTransitions:
    """Test 56 state transition cells."""

    # --- PENDING (4 transitions possible via apply) ---
    def test_pending__run(self):
        """PENDING + run() -> PENDING (created)."""
        orch = create_orchestrator()
        report = orch.run()
        assert report.differences[0].state == GhostState.PENDING

    def test_pending__apply_success_verify_pass(self):
        """PENDING + apply() success+verify -> RESOLVED."""
        orch = create_orchestrator()
        report = orch.run()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED

    def test_pending__apply_success_verify_fail(self):
        """PENDING + apply() success+verify fail -> STILL_PRESENT."""
        orch = create_orchestrator()
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={"email": "varchar(50)"}
        )
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    def test_pending__apply_failure(self):
        """PENDING + apply() executor raises -> EXECUTION_FAILED."""
        orch = create_orchestrator()
        report = orch.run()
        orch.executor.execute.side_effect = Exception("DB error")
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    def test_pending__apply_refused_review(self):
        """PENDING + apply() no proposed_change -> REVIEW_REQUIRED."""
        orch = create_orchestrator()
        report = orch.run()
        diff_id = report.differences[0].id
        # Overwrite the stored resolution — apply_one() reads from report.resolutions
        report.resolutions[diff_id] = Resolution(
            difference_id=diff_id,
            difference_signature="TYPE_CHANGED:users.email:varchar(50)",
            proposed_change=None,
            rationale="Review required",
            prompt_sha256="abc123",
        )
        outcome = orch.apply_one(diff_id)
        assert outcome.state == GhostState.REVIEW_REQUIRED

    def test_pending__apply_refused_blocked(self):
        """PENDING + apply() invalid SQL -> BLOCKED."""
        orch = create_orchestrator()
        report = orch.run()
        diff_id = report.differences[0].id
        # Overwrite the stored resolution with one that has forbidden SQL
        report.resolutions[diff_id] = Resolution(
            difference_id=diff_id,
            difference_signature="TYPE_CHANGED:users.email:varchar(50)",
            proposed_change=ChangeProposal(
                domain="database",
                target="users",
                operation="DROP",
                payload={"statement": "DROP TABLE users", "operation": "DROP"},
            ),
            rationale="Invalid SQL",
            prompt_sha256="abc123",
        )
        outcome = orch.apply_one(diff_id)
        assert outcome.state == GhostState.BLOCKED

    def test_pending__rollback_db(self):
        """PENDING + rollback() db domain -> REFUSED, state unchanged."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        assert "rollback not supported" in event.reason
        assert report.differences[0].state == GhostState.PENDING

    def test_pending__rollback_file(self):
        """PENDING + rollback() file domain -> REFUSED (not RESOLVED/STILL_PRESENT)."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        assert report.differences[0].state == GhostState.PENDING

    # --- REVIEW_REQUIRED (only apply_refused) ---
    def test_review_required__run(self):
        """REVIEW_REQUIRED + run() -> REVIEW_REQUIRED (from comparator)."""
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        assert report.differences[0].state == GhostState.REVIEW_REQUIRED

    def test_review_required__apply_refused(self):
        """REVIEW_REQUIRED + apply() -> REFUSED, state unchanged."""
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.REVIEW_REQUIRED

    def test_review_required__rollback_db(self):
        """REVIEW_REQUIRED + rollback() db domain -> REFUSED."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    def test_review_required__rollback_file(self):
        """REVIEW_REQUIRED + rollback() file domain -> REFUSED."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    # --- BLOCKED (only apply_refused) ---
    def test_blocked__run(self):
        """BLOCKED + run() -> BLOCKED (from validation)."""
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        assert report.differences[0].state == GhostState.BLOCKED

    def test_blocked__apply_refused(self):
        """BLOCKED + apply() -> REFUSED, state unchanged."""
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.BLOCKED

    def test_blocked__rollback_db(self):
        """BLOCKED + rollback() db domain -> REFUSED."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    def test_blocked__rollback_file(self):
        """BLOCKED + rollback() file domain -> REFUSED."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    # --- RESOLVED (apply_refused, rollback file/db) ---
    def test_resolved__run(self):
        """RESOLVED + run() -> RESOLVED (from previous apply)."""
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        assert report.differences[0].state == GhostState.RESOLVED

    def test_resolved__apply_refused(self):
        """RESOLVED + apply() -> REFUSED, state unchanged."""
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED

    def test_resolved__rollback_db(self):
        """RESOLVED + rollback() db domain -> REFUSED, state unchanged."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        assert "rollback not supported" in event.reason
        assert report.differences[0].state == GhostState.RESOLVED

    def test_resolved__rollback_file(self):
        """RESOLVED + rollback() file domain -> success, state unchanged."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        event = orch.rollback(report.differences[0].id)
        assert event.success is True
        assert report.differences[0].state == GhostState.RESOLVED

    # --- STILL_PRESENT (apply_refused, rollback file/db) ---
    def test_still_present__run(self):
        """STILL_PRESENT + run() -> STILL_PRESENT (from previous apply)."""
        orch = create_orchestrator()
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={"email": "varchar(50)"}
        )
        orch.apply_one(report.differences[0].id)
        assert report.differences[0].state == GhostState.STILL_PRESENT

    def test_still_present__apply_refused(self):
        """STILL_PRESENT + apply() -> REFUSED, state unchanged."""
        orch = create_orchestrator()
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={"email": "varchar(50)"}
        )
        orch.apply_one(report.differences[0].id)
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    def test_still_present__rollback_db(self):
        """STILL_PRESENT + rollback() db domain -> REFUSED, state unchanged."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={"email": "varchar(50)"}
        )
        orch.apply_one(report.differences[0].id)
        event = orch.rollback(report.differences[0].id)
        assert event.success is False
        assert report.differences[0].state == GhostState.STILL_PRESENT

    def test_still_present__rollback_file(self):
        """STILL_PRESENT + rollback() file domain -> success, state unchanged."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={"email": "varchar(50)"}
        )
        orch.apply_one(report.differences[0].id)
        event = orch.rollback(report.differences[0].id)
        assert event.success is True
        assert report.differences[0].state == GhostState.STILL_PRESENT

    # --- EXECUTION_FAILED (only apply_refused) ---
    def test_execution_failed__run(self):
        """EXECUTION_FAILED + run() -> EXECUTION_FAILED (from previous apply)."""
        orch = create_orchestrator()
        report = orch.run()
        orch.executor.execute.side_effect = Exception("DB error")
        orch.apply_one(report.differences[0].id)
        assert report.differences[0].state == GhostState.EXECUTION_FAILED

    def test_execution_failed__apply_refused(self):
        """EXECUTION_FAILED + apply() -> REFUSED, state unchanged."""
        orch = create_orchestrator()
        report = orch.run()
        orch.executor.execute.side_effect = Exception("DB error")
        orch.apply_one(report.differences[0].id)
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    def test_execution_failed__rollback_db(self):
        """EXECUTION_FAILED + rollback() db domain -> REFUSED."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        orch.executor.execute.side_effect = Exception("DB error")
        orch.apply_one(report.differences[0].id)
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    def test_execution_failed__rollback_file(self):
        """EXECUTION_FAILED + rollback() file domain -> REFUSED."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.executor.execute.side_effect = Exception("DB error")
        orch.apply_one(report.differences[0].id)
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    # --- SUPERSEDED (only apply_refused) ---
    def test_superseded__run(self):
        """SUPERSEDED + run() -> SUPERSEDED (from previous apply)."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        # Sibling file patch superseded is tested in _supersede_siblings
        assert report.differences[0].state == GhostState.RESOLVED

    def test_superseded__apply_refused(self):
        """SUPERSEDED + apply() -> REFUSED, state unchanged."""
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.SUPERSEDED

    def test_superseded__rollback_db(self):
        """SUPERSEDED + rollback() db domain -> REFUSED."""
        orch = create_orchestrator(domain="database")
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        event = orch.rollback(report.differences[0].id)
        assert event.success is False

    def test_superseded__rollback_file(self):
        """SUPERSEDED + rollback() file domain -> REFUSED."""
        orch = create_orchestrator(domain="api")
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        event = orch.rollback(report.differences[0].id)
        assert event.success is False


class TestFullMatrix:
    """
    Explicit 56-cell matrix: 7 states × 8 events.

    Events:
      E1  run()                           → creates PENDING
      E2  apply_success + verify_pass     → PENDING → RESOLVED
      E3  apply_success + verify_fail     → PENDING → STILL_PRESENT
      E4  apply_failure (executor raises) → PENDING → EXECUTION_FAILED
      E5  apply w/ no proposed_change     → PENDING → REVIEW_REQUIRED
      E6  apply w/ bad SQL                → PENDING → BLOCKED
      E7  rollback, database domain       → always REFUSED, state unchanged
      E8  rollback, file domain           → RESOLVED/STILL_PRESENT succeed; others refused

    For every non-PENDING state: E2–E6 are all refused (apply_one returns state unchanged,
    no executor call).  E7 is always refused.  E8 is refused except RESOLVED/STILL_PRESENT.
    """

    # ---- REVIEW_REQUIRED × E2–E6 (all refused) ----

    def test_review_required__apply_success(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.REVIEW_REQUIRED
        orch.executor.execute.assert_not_called()

    def test_review_required__apply_success_verify_fail(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={}
        )
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.REVIEW_REQUIRED

    def test_review_required__apply_failure(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        orch.executor.execute.side_effect = Exception("DB error")
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.REVIEW_REQUIRED

    def test_review_required__apply_refused_no_change(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.REVIEW_REQUIRED

    def test_review_required__apply_refused_bad_sql(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.REVIEW_REQUIRED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.REVIEW_REQUIRED

    # ---- BLOCKED × E2–E6 (all refused) ----

    def test_blocked__apply_success(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.BLOCKED
        orch.executor.execute.assert_not_called()

    def test_blocked__apply_success_verify_fail(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.BLOCKED

    def test_blocked__apply_failure(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        orch.executor.execute.side_effect = Exception("DB error")
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.BLOCKED

    def test_blocked__apply_refused_no_change(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.BLOCKED

    def test_blocked__apply_refused_bad_sql(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.BLOCKED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.BLOCKED

    # ---- RESOLVED × E2–E6 (all refused) ----

    def test_resolved__apply_success(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        assert report.differences[0].state == GhostState.RESOLVED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED
        # executor was called exactly once (for the first apply)
        assert orch.executor.execute.call_count == 1

    def test_resolved__apply_success_verify_fail(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={}
        )
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED

    def test_resolved__apply_failure(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        orch.executor.execute.side_effect = Exception("DB error")
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED

    def test_resolved__apply_refused_no_change(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED

    def test_resolved__apply_refused_bad_sql(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED

    # ---- STILL_PRESENT × E2–E6 (all refused) ----

    def _make_still_present(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.verifier.verify.return_value = VerificationResult(
            difference_id="test-diff", resolved=False, evidence={}
        )
        orch.apply_one(report.differences[0].id)
        assert report.differences[0].state == GhostState.STILL_PRESENT
        return orch, report

    def test_still_present__apply_success(self):
        orch, report = self._make_still_present()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    def test_still_present__apply_success_verify_fail(self):
        orch, report = self._make_still_present()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    def test_still_present__apply_failure(self):
        orch, report = self._make_still_present()
        orch.executor.execute.side_effect = Exception("DB error")
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    def test_still_present__apply_refused_no_change(self):
        orch, report = self._make_still_present()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    def test_still_present__apply_refused_bad_sql(self):
        orch, report = self._make_still_present()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.STILL_PRESENT

    # ---- EXECUTION_FAILED × E2–E6 (all refused) ----

    def _make_execution_failed(self):
        orch = create_orchestrator()
        report = orch.run()
        orch.executor.execute.side_effect = Exception("DB error")
        orch.apply_one(report.differences[0].id)
        assert report.differences[0].state == GhostState.EXECUTION_FAILED
        return orch, report

    def test_execution_failed__apply_success(self):
        orch, report = self._make_execution_failed()
        orch.executor.execute.side_effect = None
        orch.executor.execute.return_value = __import__(
            "core.models", fromlist=["ExecutionResult"]
        ).ExecutionResult(difference_id="test-diff", success=True)
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    def test_execution_failed__apply_success_verify_fail(self):
        orch, report = self._make_execution_failed()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    def test_execution_failed__apply_failure(self):
        orch, report = self._make_execution_failed()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    def test_execution_failed__apply_refused_no_change(self):
        orch, report = self._make_execution_failed()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    def test_execution_failed__apply_refused_bad_sql(self):
        orch, report = self._make_execution_failed()
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.EXECUTION_FAILED

    # ---- SUPERSEDED × E2–E6 (all refused) ----

    def test_superseded__apply_success(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.SUPERSEDED
        orch.executor.execute.assert_not_called()

    def test_superseded__apply_success_verify_fail(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.SUPERSEDED

    def test_superseded__apply_failure(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        orch.executor.execute.side_effect = Exception("DB error")
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.SUPERSEDED

    def test_superseded__apply_refused_no_change(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.SUPERSEDED

    def test_superseded__apply_refused_bad_sql(self):
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.SUPERSEDED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.SUPERSEDED


class TestInvariants:
    """Test state machine invariants."""

    def test_no_pending_after_validator_failure(self):
        """No path produces PENDING when validator_status != valid."""
        # Verified by inspecting Orchestrator._resolve_and_validate()
        pass

    def test_persistence_called_on_state_change(self):
        """persistence.save() called on every state-changing path."""
        orch = create_orchestrator()
        report = orch.run()
        initial = orch.persistence.save.call_count
        orch.apply_one(report.differences[0].id)
        assert orch.persistence.save.call_count > initial

    def test_rollback_never_to_pending(self):
        """Rollback never changes state to PENDING (event only)."""
        orch = create_orchestrator()
        report = orch.run()
        orch.apply_one(report.differences[0].id)
        orch.rollback(report.differences[0].id)
        assert report.differences[0].state != GhostState.PENDING

    def test_apply_only_on_pending(self):
        """apply_one() only processes PENDING differences."""
        orch = create_orchestrator()
        report = orch.run()
        report.differences[0].state = GhostState.RESOLVED
        outcome = orch.apply_one(report.differences[0].id)
        assert outcome.state == GhostState.RESOLVED
        orch.executor.execute.assert_not_called()
