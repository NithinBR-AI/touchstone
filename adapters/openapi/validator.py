"""
adapters/openapi/validator.py

OpenAPI contract domain validator — implements the core Validator protocol.

Two-stage validation for API drift:
  1. Here, at scan and apply time: the proposal is metadata only (which file,
     which fields). This validator decides WHICH FILE may change and for WHICH
     FIELDS, bound to the difference. It never sees the patch itself.
  2. At Exorcise time, ContractExecutor asks DeepSeek for the patch and Qwen
     checks it. If Qwen rejects it twice, a draft PR is opened for a human and
     the outcome is a failure, not a success.
A human reviews and merges every PR; nothing changes in the live API or the
repo's default branch before that.

Checks here:
  · operation is FILE_PATCH
  · the difference kind maps to a file the executor may patch, and the
    proposal's file_name is exactly that file (no arbitrary paths)
  · the proposal targets the difference's target
  · the proposal names at least one field
  · review-required kinds need human authorization
"""

from __future__ import annotations

from core.models import ChangeProposal, Difference, GhostState, ValidationResult

# The only file each auto-patchable kind may change. Mirrors ContractExecutor.
KIND_TO_FILE: dict[str, str] = {
    "FIELD_EXTRA": "openapi.yaml",
    "FIELD_UNDOCUMENTED": "openapi.yaml",
    "FIELD_MISSING": "main.py",
}

# Kinds that need a human click (human_authorized=True) before a PR may open
REVIEW_REQUIRED_KINDS = {"FIELD_MISSING"}


def _blocked(reason: str) -> ValidationResult:
    return ValidationResult(
        status="blocked", reason=reason, ghost_state=GhostState.BLOCKED
    )


class ContractValidator:
    """
    Implements the Validator protocol for the OpenAPI contract domain.
    Binds the proposal to the difference: right operation, right file, right
    target, at least one field.
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
            return _blocked(
                f"contract executor requires FILE_PATCH operation, got '{proposal.operation}'"
            )

        # --- 2. Kind must map to a patchable file ---
        allowed_file = KIND_TO_FILE.get(difference.kind)
        if allowed_file is None:
            return _blocked(
                f"contract difference kind '{difference.kind}' has no automated fix"
            )

        # --- 3. The proposal may only touch that file ---
        file_name = proposal.payload.get("file_name", "")
        if file_name != allowed_file:
            return _blocked(
                f"'{difference.kind}' may only patch '{allowed_file}', proposal names '{file_name}'"
            )

        # --- 4. Target must match the difference ---
        if proposal.target != difference.target:
            return ValidationResult(
                status="invalid",
                reason=f"proposal targets '{proposal.target}' but difference targets '{difference.target}'",
                ghost_state=GhostState.BLOCKED,
            )

        # --- 5. At least one field must be named ---
        fields = proposal.payload.get("fields")
        if not isinstance(fields, list) or not any(
            isinstance(f, str) and f for f in fields
        ):
            return _blocked("proposal payload names no fields to patch")

        # --- 6. Review-required kinds need a human click ---
        if difference.kind in REVIEW_REQUIRED_KINDS and not human_authorized:
            return ValidationResult(
                status="review_required",
                reason=f"'{difference.kind}' requires human review",
                ghost_state=GhostState.REVIEW_REQUIRED,
            )

        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)
