"""
adapters/openapi/verifier.py — Req 23

Verifier for API contract drift.

API fixes are pull requests, not in-place changes: the fix only reaches the
default branch when a human merges the PR. So this verifier does not claim the
gap is gone. It records that a PR was opened and is waiting for review, and the
evidence says so plainly. A re-scan after the merge confirms the fix: the ghost
no longer appears.
"""

from __future__ import annotations

from core.models import ComparisonTarget, Difference, VerificationResult


class ContractVerifier:
    """Verifier protocol implementation for the OpenAPI contract domain."""

    def verify(
        self,
        target: ComparisonTarget,
        difference: Difference,
    ) -> VerificationResult:
        """
        Called only after the executor opened a validated (non-draft) PR.
        resolved=True means "fix proposed as a PR"; the evidence records that it
        is not merged yet, and that the live system has not been re-read.
        """
        openapi_url = (target.context or {}).get("openapi_url", "")
        return VerificationResult(
            difference_id=difference.id,
            resolved=True,
            evidence={
                "reason": "pr_opened_not_merged",
                "note": (
                    "The fix is an open pull request. It takes effect when a human "
                    "merges it; a re-scan after the merge confirms the ghost is gone."
                ),
                "live_system_reread": False,
                "openapi_url": openapi_url,
            },
        )
