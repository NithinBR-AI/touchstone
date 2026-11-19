"""
apply_batch must enforce the same gates as apply_one.

Regression tests for a validation bypass found in review: apply_batch used to
send every eligible difference to BatchExecutor.execute_batch(), including
ones with no proposal and ones the validator rejects. Now only validated items
reach the executor, and mixed batches execute only the valid items.

In-memory domain, no network.
"""

from __future__ import annotations

from pathlib import Path

from core.cache import Cache
from core.models import (
    ChangeProposal,
    ComparisonTarget,
    Difference,
    ExecutionResult,
    GhostState,
    Report,
    Resolution,
    State,
    ValidationResult,
    VerificationResult,
)
from core.orchestrator import Orchestrator
from core.persistence import Persistence

DOMAIN = "batchtest"


class _Source:
    def targets(self) -> list[ComparisonTarget]:
        return [ComparisonTarget(domain=DOMAIN, target="t")]


class _Adapter:
    def intended(self, target: ComparisonTarget) -> State:
        return State(domain=DOMAIN, data={})

    def actual(self, target: ComparisonTarget) -> State:
        return State(domain=DOMAIN, data={})


class _Comparator:
    def compare(self, intended: State, actual: State) -> list[Difference]:
        return []


class _Resolver:
    def resolve(self, difference: Difference) -> Resolution:
        return Resolution(
            difference_id=difference.id,
            difference_signature=difference.signature,
            proposed_change=None,
        )


class _Validator:
    """Accepts only proposals whose payload says ok=True, regardless of authorization."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def validate(
        self,
        proposal: ChangeProposal,
        difference: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        self.calls.append((difference.id, human_authorized))
        if proposal.payload.get("ok"):
            return ValidationResult(status="valid", ghost_state=GhostState.PENDING)
        return ValidationResult(
            status="blocked", reason="rejected by test", ghost_state=GhostState.BLOCKED
        )


class _BatchExecutor:
    """Records exactly which differences it was asked to execute."""

    def __init__(self) -> None:
        self.received: list[str] = []

    def execute(
        self, resolution: Resolution, difference: Difference
    ) -> ExecutionResult:
        raise AssertionError("batch path must use execute_batch")

    def execute_batch(
        self, items: list[tuple[Resolution, Difference]]
    ) -> list[ExecutionResult]:
        self.received.extend(d.id for _, d in items)
        return [ExecutionResult(difference_id=d.id, success=True) for _, d in items]


class _Verifier:
    def verify(
        self, target: ComparisonTarget, difference: Difference
    ) -> VerificationResult:
        return VerificationResult(difference_id=difference.id, resolved=True)


def _diff(name: str, state: GhostState) -> Difference:
    return Difference(
        signature=f"sig-{name}",
        domain=DOMAIN,
        kind="KIND",
        target=name,
        severity="LOW",
        state=state,
    )


def _resolution(diff: Difference, ok: bool | None) -> Resolution:
    proposal = (
        None
        if ok is None
        else ChangeProposal(
            domain=DOMAIN, target=diff.target, operation="OP", payload={"ok": ok}
        )
    )
    return Resolution(
        difference_id=diff.id,
        difference_signature=diff.signature,
        proposed_change=proposal,
    )


def _orch(tmp_path: Path, diffs_and_ok: list[tuple[Difference, bool | None]]):
    executor, validator = _BatchExecutor(), _Validator()
    orch = Orchestrator(
        input_source=_Source(),
        source_adapter=_Adapter(),
        comparator=_Comparator(),
        resolver=_Resolver(),
        executor=executor,
        verifier=_Verifier(),
        validator=validator,
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / "cache.json"),
        domain=DOMAIN,
    )
    report = Report(domain=DOMAIN)
    for diff, ok in diffs_and_ok:
        report.differences.append(diff)
        report.resolutions[diff.id] = _resolution(diff, ok)
    orch._report = report
    return orch, executor, validator


def test_missing_proposal_never_reaches_executor(tmp_path):
    d = _diff("a", GhostState.BLOCKED)
    orch, executor, _ = _orch(tmp_path, [(d, None)])
    outcomes = orch.apply_batch([d.id], human_authorized=True)
    assert executor.received == []
    assert outcomes[0].state == GhostState.REVIEW_REQUIRED
    assert d.state == GhostState.REVIEW_REQUIRED


def test_no_resolution_at_all_never_reaches_executor(tmp_path):
    d = _diff("a", GhostState.PENDING)
    orch, executor, _ = _orch(tmp_path, [])
    orch.report.differences.append(d)
    outcomes = orch.apply_batch([d.id])
    assert executor.received == []
    assert outcomes[0].state == GhostState.REVIEW_REQUIRED


def test_rejected_proposal_never_reaches_executor_even_when_authorized(tmp_path):
    d = _diff("a", GhostState.BLOCKED)
    orch, executor, validator = _orch(tmp_path, [(d, False)])
    outcomes = orch.apply_batch([d.id], human_authorized=True)
    assert executor.received == []
    assert validator.calls == [(d.id, True)]
    assert outcomes[0].state == GhostState.BLOCKED


def test_valid_proposal_reaches_executor_and_resolves(tmp_path):
    d = _diff("a", GhostState.PENDING)
    orch, executor, validator = _orch(tmp_path, [(d, True)])
    outcomes = orch.apply_batch([d.id])
    assert executor.received == [d.id]
    assert validator.calls == [(d.id, False)]
    assert outcomes[0].state == GhostState.RESOLVED


def test_mixed_batch_executes_only_validated_items(tmp_path):
    good = _diff("good", GhostState.BLOCKED)
    bad = _diff("bad", GhostState.BLOCKED)
    none = _diff("none", GhostState.BLOCKED)
    orch, executor, _ = _orch(tmp_path, [(good, True), (bad, False), (none, None)])
    outcomes = orch.apply_batch([good.id, bad.id, none.id], human_authorized=True)
    assert executor.received == [good.id]
    states = {o.difference_id: o.state for o in outcomes}
    assert states == {
        good.id: GhostState.RESOLVED,
        bad.id: GhostState.BLOCKED,
        none.id: GhostState.REVIEW_REQUIRED,
    }


def test_unauthorized_blocked_diff_is_not_even_validated(tmp_path):
    d = _diff("a", GhostState.BLOCKED)
    orch, executor, validator = _orch(tmp_path, [(d, True)])
    outcomes = orch.apply_batch([d.id])
    assert executor.received == [] and validator.calls == []
    assert outcomes[0].state == GhostState.BLOCKED
