"""
Core protocols — the interfaces every domain adapter must satisfy.

No domain-specific logic lives here. Adapters import from core;
core never imports from adapters.

Base protocols (all domains implement):
  InputSource, SourceAdapter, Comparator, Resolver, Executor, Verifier, Validator

Optional capability protocols (implement only if the domain supports it):
  BatchExecutor  — executor can process multiple diffs in one call
  RollbackSupport — executor can reverse a previously applied change

The orchestrator uses isinstance() checks for optional protocols.
An executor that implements neither is still a valid Executor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.models import (
    ChangeProposal,
    ComparisonTarget,
    Difference,
    ExecutionResult,
    Resolution,
    State,
    ValidationResult,
    VerificationResult,
)


@runtime_checkable
class InputSource(Protocol):
    """Discovers or accepts comparison targets."""

    def targets(self) -> list[ComparisonTarget]:
        """Return the list of targets to compare."""
        ...


@runtime_checkable
class SourceAdapter(Protocol):
    """
    Reads both sides of a comparison for a single target and returns
    normalised state objects that the Comparator can diff.
    """

    def intended(self, target: ComparisonTarget) -> State:
        """Return the declared/intended state (from repo files)."""
        ...

    def actual(self, target: ComparisonTarget) -> State:
        """Return the live/actual state (from running system)."""
        ...


@runtime_checkable
class Comparator(Protocol):
    """Diffs intended vs actual state and emits Differences."""

    def compare(self, intended: State, actual: State) -> list[Difference]:
        """Return one Difference per detected ghost."""
        ...


@runtime_checkable
class Resolver(Protocol):
    """
    Proposes one fix per Difference.
    Cache hits bypass the live call.
    Returns Resolution with proposed_change=None when no fix is available.
    """

    def resolve(self, difference: Difference) -> Resolution:
        """Return a Resolution (proposed_change may be None for review-only)."""
        ...


@runtime_checkable
class Validator(Protocol):
    """
    Validates a proposed ChangeProposal against the detected Difference.

    Each domain provides its own implementation.
    The core orchestrator invokes the injected validator before authorization
    and execution. A missing or failed validation never silently becomes valid.

    Responsibilities:
      · Check the proposal is structurally sound.
      · Verify the proposal targets what the difference says it targets.
      · Apply domain-specific safety constraints.
      · Return structured evidence and rejection reasons.
    """

    def validate(
        self,
        proposal: ChangeProposal,
        difference: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        """Validate proposal against difference. Never raises — returns ValidationResult."""
        ...


@runtime_checkable
class Executor(Protocol):
    """Applies a validated Resolution atomically."""

    def execute(self, resolution: Resolution, difference: Difference) -> ExecutionResult:
        """Apply the proposed change. Raise on unrecoverable failure."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Re-reads state after execution to confirm the ghost is gone."""

    def verify(
        self,
        target: ComparisonTarget,
        difference: Difference,
    ) -> VerificationResult:
        """Return evidence-backed verification result."""
        ...


# ---------------------------------------------------------------------------
# Optional capability protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class BatchExecutor(Protocol):
    """
    Optional: executor can process multiple diffs in a single call.

    Useful when grouping reduces API calls (e.g. one PR per kind).
    Orchestrator checks isinstance(executor, BatchExecutor) before calling.
    Executors that do not implement this fall back to sequential apply_one.
    """

    def execute_batch(
        self,
        items: list[tuple[Resolution, Difference]],
    ) -> list[ExecutionResult]:
        """
        Apply multiple diffs. Returns one ExecutionResult per input item.
        Order of results need not match input order — match by difference_id.
        """
        ...


@runtime_checkable
class RollbackSupport(Protocol):
    """
    Optional: executor can reverse a previously applied change.

    Executors that do not implement this protocol do not support rollback.
    The orchestrator checks isinstance(executor, RollbackSupport) before
    attempting rollback — no domain strings required.
    """

    def rollback(self, execution: ExecutionResult) -> bool:
        """
        Attempt to reverse the applied change.
        Returns True on success, False on failure. Never raises.
        """
        ...
