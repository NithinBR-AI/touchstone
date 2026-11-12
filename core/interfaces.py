"""
Core protocols — the six interfaces every domain adapter must satisfy.

No domain-specific logic lives here. Adapters import from core;
core never imports from adapters.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.models import (ComparisonTarget, Difference, ExecutionResult,
                         Resolution, State, VerificationResult)


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
    Proposes one fix per Difference via an LLM agent.
    Cache hits bypass the live call.
    """

    def resolve(self, difference: Difference) -> Resolution:
        """Return a Resolution (proposed_change may be None for review-only)."""
        ...


@runtime_checkable
class Executor(Protocol):
    """Applies a validated Resolution atomically."""

    def execute(
        self, resolution: Resolution, difference: Difference
    ) -> ExecutionResult:
        """Apply the proposed change. Raise on failure."""
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
