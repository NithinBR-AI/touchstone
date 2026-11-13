"""
adapters/redis/validator.py

Redis cache domain validator — implements the core Validator protocol.

Validates a ChangeProposal for cache drift differences.
Checks:
  · operation is one the cache executor handles
  · target in proposal matches the difference target
  · payload contains required fields for the operation
"""

from __future__ import annotations

from core.models import ChangeProposal, Difference, GhostState, ValidationResult

# Kinds the cache executor handles
EXECUTABLE_KINDS = {"KEY_MISSING", "UNDECLARED_KEY"}


class CacheValidator:
    """
    Implements the Validator protocol for the Redis cache domain.

    Validates that the proposal operation and target are consistent
    with the detected cache difference.
    """

    def validate(
        self,
        proposal: ChangeProposal,
        difference: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        # --- 1. Kind must be one the executor handles ---
        if difference.kind not in EXECUTABLE_KINDS:
            return ValidationResult(
                status="blocked",
                reason=f"cache executor does not handle kind '{difference.kind}'",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 2. Operation must match kind ---
        expected_op = "CACHE_WARM" if difference.kind == "KEY_MISSING" else "CACHE_DELETE"
        if proposal.operation != expected_op:
            return ValidationResult(
                status="blocked",
                reason=f"expected operation '{expected_op}' for kind '{difference.kind}', got '{proposal.operation}'",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 3. Target must match ---
        proposal_target = proposal.payload.get("key", proposal.target)
        if proposal_target != difference.target:
            return ValidationResult(
                status="invalid",
                reason=f"proposal targets key '{proposal_target}' but difference targets '{difference.target}'",
                ghost_state=GhostState.BLOCKED,
            )

        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)
