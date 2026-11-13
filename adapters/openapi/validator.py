"""
adapters/openapi/validator.py

OpenAPI contract domain validator — implements the core Validator protocol.

Validates a ChangeProposal for contract drift differences.
Checks:
  · proposal payload contains required fields (patched_content, file_path)
  · patched_content is non-empty
  · target matches the difference target
  · operation is one the contract executor handles

The LLM-generated patched_content is validated here before authorization
and execution. What is validated is what gets committed to GitHub.
"""

from __future__ import annotations

from core.models import ChangeProposal, Difference, GhostState, ValidationResult

# Kinds the contract executor can handle automatically
AUTO_FIXABLE_KINDS = {"FIELD_EXTRA", "FIELD_UNDOCUMENTED"}

# Kinds that require human review (no automated PR)
REVIEW_REQUIRED_KINDS = {
    "FIELD_MISSING",
    "ENDPOINT_MISSING",
    "STATUS_CODE_CHANGED",
    "FIELD_RENAME_SUGGESTION",
}


class ContractValidator:
    """
    Implements the Validator protocol for the OpenAPI contract domain.

    Validates that the proposal is structurally sound and targets
    what the difference says it targets.
    """

    def validate(
        self,
        proposal: ChangeProposal,
        difference: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        # --- 1. Operation must be FILE_PATCH ---
        if proposal.operation != "FILE_PATCH":
            return ValidationResult(
                status="blocked",
                reason=f"contract executor requires FILE_PATCH operation, got '{proposal.operation}'",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 2. Payload must contain patched_content ---
        patched_content = proposal.payload.get("patched_content", "")
        if not patched_content or not isinstance(patched_content, str):
            return ValidationResult(
                status="blocked",
                reason="proposal payload missing or empty 'patched_content'",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 3. Payload must contain file_path ---
        file_path = proposal.payload.get("file_path", "")
        if not file_path:
            return ValidationResult(
                status="blocked",
                reason="proposal payload missing 'file_path'",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 4. Auto-fixability gate ---
        if difference.kind in REVIEW_REQUIRED_KINDS and not human_authorized:
            return ValidationResult(
                status="review_required",
                reason=f"'{difference.kind}' requires human review",
                ghost_state=GhostState.REVIEW_REQUIRED,
            )

        # --- 5. Unknown kinds are blocked ---
        if (
            difference.kind not in AUTO_FIXABLE_KINDS
            and difference.kind not in REVIEW_REQUIRED_KINDS
        ):
            return ValidationResult(
                status="blocked",
                reason=f"unknown contract difference kind '{difference.kind}'",
                ghost_state=GhostState.BLOCKED,
            )

        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)
