"""
Core orchestrator — sole owner of the state machine.

Implements the 10-step compare-verify workflow:
  1. Read targets
  2. Read both sides, normalise into State
  3. Compare → classify each Difference
  4. Resolver proposes one fix per ghost
  5. Validator applies structural + semantic gates
  6. Human approves → Executor applies atomically
  7. Verifier re-reads state → RESOLVED, STILL_PRESENT, or EXECUTION_FAILED

State machine transitions (only paths the orchestrator produces):
  PENDING         → EXECUTION_FAILED   (executor raises)
  PENDING         → RESOLVED           (executor succeeds + verify passes)
  PENDING         → STILL_PRESENT      (executor succeeds + verify fails)
  PENDING         → SUPERSEDED         (sibling file patch applied first)
  REVIEW_REQUIRED → apply refused
  BLOCKED         → apply refused
  EXECUTION_FAILED→ apply refused
  RESOLVED (file) → rollback recorded, state unchanged
  RESOLVED (db)   → rollback refused
  STILL_PRESENT   → same as RESOLVED for rollback handling

No path produces PENDING when validator_status != "valid".
persistence.save() is called on every state-changing path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.cache import Cache
from core.interfaces import (Comparator, Executor, InputSource, Resolver,
                             SourceAdapter, Verifier)
from core.models import (ApplyOutcome, ComparisonTarget, Difference,
                         GhostState, Report, Resolution, RollbackEvent,
                         VerificationResult)
from core.persistence import Persistence
from core.validator import SQLChange, SQLValidator, ValidationResult


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
        validator_sql: SQLValidator,
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
        self.validator_sql = validator_sql
        self.persistence = persistence
        self.cache = cache
        self.domain = domain
        self.principal = principal

        # Active report (reset on each run())
        self._report: Report | None = None

    # ------------------------------------------------------------------
    # Public API (used by FastAPI endpoints)
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
                # Merge target-level context (e.g. repo, openapi_url) into each
                # diff's context so resolvers and executors can access it.
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

        Returns ApplyOutcome.
        Normal path: only PENDING diffs may be applied.
        Human-authorized path (human_authorized=True): also accepts REVIEW_REQUIRED
          diffs that have a proposed_change — the human has seen the LLM proposal
          and clicked Exorcise to authorize it.
        """
        report = self._require_report()
        diff = self._get_difference(report, difference_id)

        # Gate: PENDING always allowed; REVIEW_REQUIRED and BLOCKED allowed when
        # human_authorized — human has seen the proposal and clicked Exorcise.
        if diff.state == GhostState.PENDING:
            pass  # normal auto-fix path
        elif (
            diff.state in (GhostState.REVIEW_REQUIRED, GhostState.BLOCKED)
            and human_authorized
        ):
            pass  # human clicked Exorcise — authorized
        else:
            return ApplyOutcome(
                difference_id=difference_id,
                state=diff.state,
            )

        resolution = report.resolutions.get(difference_id)

        # Build a stub resolution if none exists (e.g. cache domain diffs where
        # the executor derives the action from the Difference itself, not a SQL change)
        if resolution is None:
            resolution = Resolution(
                difference_id=difference_id,
                difference_signature=diff.signature,
                proposed_change=None,
            )

        # When no proposed change is available and not human-authorized: mark REVIEW_REQUIRED.
        # When human-authorized with no proposed change: proceed to execution — the executor
        # may handle it directly from the Difference (e.g. cache domain warm call).
        if resolution.proposed_change is None and not human_authorized:
            diff.state = GhostState.REVIEW_REQUIRED
            self.persistence.save(report)
            return ApplyOutcome(
                difference_id=difference_id,
                state=GhostState.REVIEW_REQUIRED,
            )

        # Validate again (defence-in-depth — validator is the safety boundary).
        # Only validate when there is a proposed SQL/file change to validate.
        if resolution.proposed_change is not None:
            validation = self._validate_change(
                resolution, diff, human_authorized=human_authorized
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
            # Cache only verified fixes — this is the proof the fix actually worked
            self.cache.put(resolution)
            # Supersede other PENDING diffs on the same file
            if isinstance(resolution.proposed_change, type(None)) is False:
                from core.models import FilePatch

                if isinstance(resolution.proposed_change, FilePatch):
                    self._supersede_siblings(
                        report, difference_id, resolution.proposed_change.file_path
                    )
                    report.requires_refresh = True
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
        Apply multiple diffs in a single batch — one executor call per group.

        Used by App 2 (Contract Observer) to open one PR per kind rather than
        one PR per field. The executor.execute_batch() method handles grouping
        and idempotency internally.

        Each diff must be PENDING or (REVIEW_REQUIRED with human_authorized=True).
        Diffs in other states are returned with their current state unchanged.
        """
        report = self._require_report()
        outcomes: list[ApplyOutcome] = []

        # Partition: eligible vs refused
        eligible: list[Difference] = []
        for did in difference_ids:
            diff = self._get_difference(report, did)
            if diff.state == GhostState.PENDING:
                eligible.append(diff)
            elif (
                diff.state in (GhostState.REVIEW_REQUIRED, GhostState.BLOCKED)
                and human_authorized
            ):
                eligible.append(diff)
            else:
                outcomes.append(ApplyOutcome(difference_id=did, state=diff.state))

        if not eligible:
            return outcomes

        # Build resolution stubs for the batch — executor adapters that do not
        # use the resolution (ContractExecutor) will ignore it. Those that do
        # (SQLExecutor) are not expected to be called via apply_batch.
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

        # Delegate to executor — ContractExecutor.execute_batch takes list[(Resolution, Difference)]
        # and handles its own grouping by kind.
        batch_items = [(resolution_stubs[d.id], d) for d in eligible]
        try:
            exec_results = self.executor.execute_batch(batch_items)  # type: ignore[attr-defined]
        except AttributeError:
            # Executor does not support batch — fall back to sequential apply_one
            for diff in eligible:
                outcome = self.apply_one(diff.id, human_authorized=human_authorized)
                outcomes.append(outcome)
            return outcomes
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
                # Verify
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
        Database domain: always fails with explicit reason.
        File domain: atomic reverse-patch write.
        """
        report = self._require_report()
        diff = self._get_difference(report, difference_id)
        outcome = report.outcomes.get(difference_id)

        # Database domain — rollback not supported
        if self.domain == "database":
            event = RollbackEvent(
                difference_id=difference_id,
                success=False,
                reason="rollback not supported for database domain",
            )
            report.rollback_events.setdefault(difference_id, []).append(event)
            self._audit(report, difference_id, "rollback_refused", event.reason)
            self.persistence.save(report)
            return event

        # Only RESOLVED or STILL_PRESENT file-domain diffs can be rolled back
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

        # Delegate to executor (file domain executor implements the atomic reverse write)
        try:
            success = self.executor.rollback(outcome.execution)  # type: ignore[attr-defined]
            reason = "" if success else "executor reported rollback failure"
        except Exception as exc:
            success = False
            reason = type(exc).__name__

        event = RollbackEvent(
            difference_id=difference_id,
            success=success,
            reason=reason,
        )
        # State is UNCHANGED per spec
        report.rollback_events.setdefault(difference_id, []).append(event)
        self._audit(report, difference_id, "rollback_event", reason or "success")
        self.persistence.save(report)
        return event

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_and_validate(self, diff: Difference) -> Resolution:
        """Run resolver (with cache) then validate the proposed change."""
        import hashlib
        import json

        from core.cache import CACHE_VERSION, VALIDATOR_CONTRACT

        # Build prompt hash deterministically from difference fields
        prompt_data = json.dumps(
            {"kind": diff.kind, "target": diff.target, "context": diff.context},
            sort_keys=True,
        )
        prompt_hash = hashlib.sha256(prompt_data.encode()).hexdigest()

        # Cache lookup — only verified fixes are stored (see apply_one)
        cached = self.cache.get(diff.signature, prompt_hash)
        if cached:
            cached.difference_id = diff.id
            resolution = cached
        else:
            resolution = self.resolver.resolve(diff)
            resolution.cache_version = CACHE_VERSION
            resolution.validator_contract = VALIDATOR_CONTRACT
            resolution.prompt_hash = prompt_hash
            # Do NOT cache here — cache only after apply+verify succeeds (apply_one)

        # Validate — sets diff.state if not valid.
        # proposed_change is preserved so the human can review the SQL before
        # authorizing. apply_one re-validates with human_authorized=True.
        if resolution.proposed_change is not None:
            validation = self._validate_change(resolution, diff)
            if validation.status != "valid":
                diff.state = validation.ghost_state

        # A ghost may only stay PENDING with a validated change to apply.
        # Database domain: no usable proposal → BLOCKED (executor requires SQLChange).
        # Other domains (cache, api): proposed_change=None is expected — executor
        # derives the action from the Difference itself. Mark REVIEW_REQUIRED so
        # the UI shows an Exorcise button (human_authorized path in apply_one).
        if resolution.proposed_change is None and diff.state == GhostState.PENDING:
            if self.domain == "database":
                diff.state = GhostState.BLOCKED
            else:
                diff.state = GhostState.REVIEW_REQUIRED

        return resolution

    def _validate_change(
        self,
        resolution: Resolution,
        diff: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        change = resolution.proposed_change
        if change is None:
            return ValidationResult(
                status="review_required",
                reason="no proposed change",
                ghost_state=GhostState.REVIEW_REQUIRED,
            )
        if isinstance(change, SQLChange):
            return self.validator_sql.validate(
                change, diff, human_authorized=human_authorized
            )
        # FilePatch — no local validation; executor handles it domain-specifically
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)

    def _require_report(self) -> Report:
        if self._report is None:
            raise RuntimeError("No active report — call run() first")
        return self._report

    def _get_difference(self, report: Report, difference_id: str) -> Difference:
        for diff in report.differences:
            if diff.id == difference_id:
                return diff
        raise KeyError(f"Difference {difference_id} not found in report")

    def _find_target(self, diff: Difference) -> ComparisonTarget:
        return ComparisonTarget(
            domain=diff.domain or self.domain,
            target=diff.target,
            context=diff.context,
        )

    def _supersede_siblings(
        self, report: Report, applied_id: str, file_path: str
    ) -> None:
        """Mark other PENDING diffs on the same file as SUPERSEDED."""
        for diff in report.differences:
            if diff.id == applied_id:
                continue
            if diff.state != GhostState.PENDING:
                continue
            res = report.resolutions.get(diff.id)
            if res and res.proposed_change is not None:
                from core.models import FilePatch

                if isinstance(res.proposed_change, FilePatch):
                    if res.proposed_change.file_path == file_path:
                        diff.state = GhostState.SUPERSEDED

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
