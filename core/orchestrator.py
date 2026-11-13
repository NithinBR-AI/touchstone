"""
Core orchestrator — sole owner of the state machine.

Implements the compare-resolve-validate-authorize-execute-verify workflow:
  1. Read targets
  2. Read both sides, normalise into State
  3. Compare → classify each Difference
  4. Resolver proposes one fix per ghost
  5. Validator applies domain-specific safety gates (injected)
  6. Human approves → Executor applies atomically
  7. Verifier re-reads state → RESOLVED, STILL_PRESENT, or EXECUTION_FAILED

State machine transitions (only paths the orchestrator produces):
  PENDING         → EXECUTION_FAILED   (executor raises)
  PENDING         → RESOLVED           (executor succeeds + verify passes)
  PENDING         → STILL_PRESENT      (executor succeeds + verify fails)
  PENDING         → SUPERSEDED         (sibling patch applied first)
  REVIEW_REQUIRED → apply refused
  BLOCKED         → apply refused
  EXECUTION_FAILED→ apply refused
  RESOLVED        → rollback recorded, state unchanged
  STILL_PRESENT   → same as RESOLVED for rollback handling

No domain-specific logic lives here.
persistence.save() is called on every state-changing path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.cache import CACHE_VERSION, VALIDATOR_CONTRACT, Cache
from core.interfaces import (
    BatchExecutor,
    Comparator,
    Executor,
    InputSource,
    Resolver,
    RollbackSupport,
    SourceAdapter,
    Validator,
    Verifier,
)
from core.models import (
    ApplyOutcome,
    ChangeProposal,
    ComparisonTarget,
    Difference,
    GhostState,
    Report,
    Resolution,
    RollbackEvent,
    ValidationResult,
    VerificationResult,
)
from core.persistence import Persistence


class Orchestrator:
    """
    Domain-agnostic orchestrator.

    Each app passes its domain-specific adapters at construction time.
    The orchestrator never imports from adapters — only from core/.
    """

    def __init__(
        self,
        *,
        input_source: InputSource,
        source_adapter: SourceAdapter,
        comparator: Comparator,
        resolver: Resolver,
        executor: Executor,
        verifier: Verifier,
        validator: Validator,
        persistence: Persistence,
        cache: Cache,
        domain: str,
        principal: str = "anonymous",
    ) -> None:
        self.input_source = input_source
        self.source_adapter = source_adapter
        self.comparator = comparator
        self.resolver = resolver
        self.executor = executor
        self.verifier = verifier
        self.validator = validator
        self.persistence = persistence
        self.cache = cache
        self.domain = domain
        self.principal = principal

        self._report: Report | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def report(self) -> Report | None:
        return self._report

    def run(self, targets: list[ComparisonTarget] | None = None) -> Report:
        """
        Steps 1–4: discover → compare → resolve → validate.
        Returns a Report with all Differences and their Resolutions.
        Does NOT apply anything — apply_one() does that.
        """
        if targets is None:
            targets = self.input_source.targets()

        report = Report(domain=self.domain)
        self._report = report

        for target in targets:
            intended = self.source_adapter.intended(target)
            actual = self.source_adapter.actual(target)
            differences = self.comparator.compare(intended, actual)

            for diff in differences:
                for k, v in target.context.items():
                    diff.context.setdefault(k, v)
                report.differences.append(diff)
                resolution = self._resolve_and_validate(diff)
                report.resolutions[diff.id] = resolution

        self.persistence.save(report)
        return report

    def apply_one(
        self,
        difference_id: str,
        *,
        human_authorized: bool = False,
    ) -> ApplyOutcome:
        """
        Steps 5–7: validate → execute → verify for a single Difference.

        Normal path: only PENDING diffs may be applied.
        Human-authorized path: also accepts REVIEW_REQUIRED and BLOCKED diffs
          that have a proposed_change — human has seen the proposal and clicked
          Exorcise to authorize it.
        """
        report = self._require_report()
        diff = self._get_difference(report, difference_id)

        if diff.state == GhostState.PENDING:
            pass
        elif diff.state in (GhostState.REVIEW_REQUIRED, GhostState.BLOCKED) and human_authorized:
            pass
        else:
            return ApplyOutcome(difference_id=difference_id, state=diff.state)

        resolution = report.resolutions.get(difference_id)

        if resolution is None:
            resolution = Resolution(
                difference_id=difference_id,
                difference_signature=diff.signature,
                proposed_change=None,
            )

        # None proposal without human authorization → REVIEW_REQUIRED.
        # None proposal with human authorization → executor acts from Difference
        # (only valid for domains whose executor implements this pattern and whose
        # resolver always returns a ChangeProposal for executable diffs).
        if resolution.proposed_change is None and not human_authorized:
            diff.state = GhostState.REVIEW_REQUIRED
            self.persistence.save(report)
            return ApplyOutcome(
                difference_id=difference_id,
                state=GhostState.REVIEW_REQUIRED,
            )

        # Validate (defence-in-depth — validator is the safety boundary).
        if resolution.proposed_change is not None:
            validation = self.validator.validate(
                resolution.proposed_change, diff, human_authorized=human_authorized
            )
            if validation.status != "valid":
                diff.state = validation.ghost_state
                self._audit(report, difference_id, "apply_refused", validation.reason)
                self.persistence.save(report)
                return ApplyOutcome(
                    difference_id=difference_id,
                    state=diff.state,
                )

        # Execute
        try:
            execution = self.executor.execute(resolution, diff)
        except Exception as exc:
            diff.state = GhostState.EXECUTION_FAILED
            self._audit(report, difference_id, "execution_failed", type(exc).__name__)
            outcome = ApplyOutcome(
                difference_id=difference_id,
                state=GhostState.EXECUTION_FAILED,
                execution=None,
            )
            report.outcomes[difference_id] = outcome
            self.persistence.save(report)
            return outcome

        if not execution.success:
            diff.state = GhostState.EXECUTION_FAILED
            self._audit(report, difference_id, "execution_failed", execution.error_code)
            outcome = ApplyOutcome(
                difference_id=difference_id,
                state=GhostState.EXECUTION_FAILED,
                execution=execution,
                verification=None,
            )
            report.outcomes[difference_id] = outcome
            self.persistence.save(report)
            return outcome

        # Verify
        target = self._find_target(diff)
        verification = self.verifier.verify(target, diff)

        if verification.resolved:
            diff.state = GhostState.RESOLVED
            self.cache.put(resolution)
        else:
            diff.state = GhostState.STILL_PRESENT

        self._audit(report, difference_id, "applied", diff.state.value)
        outcome = ApplyOutcome(
            difference_id=difference_id,
            state=diff.state,
            execution=execution,
            verification=verification,
        )
        report.outcomes[difference_id] = outcome
        self.persistence.save(report)
        return outcome

    def apply_batch(
        self,
        difference_ids: list[str],
        *,
        human_authorized: bool = False,
    ) -> list[ApplyOutcome]:
        """
        Apply multiple diffs in a single batch.

        Delegates to BatchExecutor.execute_batch() if the executor supports it.
        Falls back to sequential apply_one() otherwise.
        """
        report = self._require_report()
        outcomes: list[ApplyOutcome] = []

        eligible: list[Difference] = []
        for did in difference_ids:
            diff = self._get_difference(report, did)
            if diff.state == GhostState.PENDING:
                eligible.append(diff)
            elif (
                diff.state in (GhostState.REVIEW_REQUIRED, GhostState.BLOCKED) and human_authorized
            ):
                eligible.append(diff)
            else:
                outcomes.append(ApplyOutcome(difference_id=did, state=diff.state))

        if not eligible:
            return outcomes

        resolution_stubs = {
            diff.id: (
                report.resolutions.get(diff.id)
                or Resolution(
                    difference_id=diff.id,
                    difference_signature=diff.signature,
                    proposed_change=None,
                )
            )
            for diff in eligible
        }

        if not isinstance(self.executor, BatchExecutor):
            for diff in eligible:
                outcome = self.apply_one(diff.id, human_authorized=human_authorized)
                outcomes.append(outcome)
            return outcomes

        batch_items = [(resolution_stubs[d.id], d) for d in eligible]
        try:
            exec_results = self.executor.execute_batch(batch_items)
        except Exception as exc:
            for diff in eligible:
                diff.state = GhostState.EXECUTION_FAILED
                self._audit(report, diff.id, "execution_failed", type(exc).__name__)
                outcomes.append(
                    ApplyOutcome(
                        difference_id=diff.id,
                        state=GhostState.EXECUTION_FAILED,
                    )
                )
            self.persistence.save(report)
            return outcomes

        diff_map = {d.id: d for d in eligible}
        for exec_result in exec_results:
            diff = diff_map.get(exec_result.difference_id)
            if diff is None:
                continue

            if exec_result.success:
                target = self._find_target(diff)
                verification = self.verifier.verify(target, diff)
                if verification.resolved:
                    diff.state = GhostState.RESOLVED
                    res = resolution_stubs.get(diff.id)
                    if res:
                        self.cache.put(res)
                else:
                    diff.state = GhostState.STILL_PRESENT
            else:
                diff.state = GhostState.EXECUTION_FAILED

            self._audit(report, diff.id, "applied", diff.state.value)
            outcome = ApplyOutcome(
                difference_id=diff.id,
                state=diff.state,
                execution=exec_result,
            )
            report.outcomes[diff.id] = outcome
            outcomes.append(outcome)

        self.persistence.save(report)
        return outcomes

    def rollback(self, difference_id: str) -> RollbackEvent:
        """
        Rollback is an event, not a state transition.
        Only executors implementing RollbackSupport can roll back.
        """
        report = self._require_report()
        diff = self._get_difference(report, difference_id)
        outcome = report.outcomes.get(difference_id)

        # Executor does not support rollback
        if not isinstance(self.executor, RollbackSupport):
            event = RollbackEvent(
                difference_id=difference_id,
                success=False,
                reason=f"rollback not supported for {self.domain} domain",
            )
            report.rollback_events.setdefault(difference_id, []).append(event)
            self._audit(report, difference_id, "rollback_refused", event.reason)
            self.persistence.save(report)
            return event

        if diff.state not in (GhostState.RESOLVED, GhostState.STILL_PRESENT):
            event = RollbackEvent(
                difference_id=difference_id,
                success=False,
                reason=f"rollback refused: state is {diff.state.value}",
            )
            report.rollback_events.setdefault(difference_id, []).append(event)
            self.persistence.save(report)
            return event

        if outcome is None or outcome.execution is None:
            event = RollbackEvent(
                difference_id=difference_id,
                success=False,
                reason="no execution record found",
            )
            report.rollback_events.setdefault(difference_id, []).append(event)
            self.persistence.save(report)
            return event

        try:
            success = self.executor.rollback(outcome.execution)
            reason = "" if success else "executor reported rollback failure"
        except Exception as exc:
            success = False
            reason = type(exc).__name__

        event = RollbackEvent(
            difference_id=difference_id,
            success=success,
            reason=reason,
        )
        report.rollback_events.setdefault(difference_id, []).append(event)
        self._audit(report, difference_id, "rollback_event", reason or "success")
        self.persistence.save(report)
        return event

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_and_validate(self, diff: Difference) -> Resolution:
        """Run resolver (with cache) then validate the proposed change."""
        prompt_data = json.dumps(
            {"kind": diff.kind, "target": diff.target, "context": diff.context},
            sort_keys=True,
        )
        prompt_hash = hashlib.sha256(prompt_data.encode()).hexdigest()

        cached = self.cache.get(diff.signature, prompt_hash)
        if cached:
            cached.difference_id = diff.id
            resolution = cached
        else:
            resolution = self.resolver.resolve(diff)
            resolution.cache_version = CACHE_VERSION
            resolution.validator_contract = VALIDATOR_CONTRACT
            resolution.prompt_hash = prompt_hash

        # Validate if a proposal exists
        if resolution.proposed_change is not None:
            validation = self.validator.validate(resolution.proposed_change, diff)
            if validation.status != "valid":
                diff.state = validation.ghost_state

        # No proposal → executor cannot act without human authorization
        if resolution.proposed_change is None and diff.state == GhostState.PENDING:
            diff.state = GhostState.REVIEW_REQUIRED

        return resolution

    def _find_target(self, diff: Difference) -> ComparisonTarget:
        return ComparisonTarget(
            domain=diff.domain or self.domain,
            target=diff.target,
            context=diff.context,
        )

    def _require_report(self) -> Report:
        if self._report is None:
            raise RuntimeError("No active report — call run() first")
        return self._report

    def _get_difference(self, report: Report, difference_id: str) -> Difference:
        for diff in report.differences:
            if diff.id == difference_id:
                return diff
        raise KeyError(f"Difference {difference_id} not found in report")

    def _audit(
        self,
        report: Report,
        difference_id: str,
        event: str,
        detail: str,
    ) -> None:
        report.audit_log.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "principal": self.principal,
                "difference_id": difference_id,
                "event": event,
                "detail": detail,
            }
        )
