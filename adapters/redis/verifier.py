"""
adapters/redis/verifier.py

Verifier for cache drift. Re-fetches /cache/status and checks whether
the drift is resolved.

For KEY_MISSING: key should now appear in Redis.
For UNDECLARED_KEY: not auto-fixable, verification is a no-op.
"""

from __future__ import annotations

from core.models import Difference, ExecutionResult, VerificationResult

from .actual import load_from_url as load_actual


class CacheVerifier:
    """Verifier protocol implementation for the Redis cache domain."""

    def __init__(self, cache_status_url: str) -> None:
        self.cache_status_url = cache_status_url

    def verify(
        self,
        diff: Difference,
        result: ExecutionResult,
    ) -> VerificationResult:
        if not result.success:
            return VerificationResult(
                difference_id=diff.id,
                resolved=False,
                evidence={"reason": "execution failed"},
            )

        try:
            actual_state = load_actual(self.cache_status_url)
        except Exception as exc:
            return VerificationResult(
                difference_id=diff.id,
                resolved=False,
                evidence={"reason": "fetch_failed", "error": type(exc).__name__},
            )

        actual_keys: set[str] = set(actual_state.data.get("keys", []))

        if diff.kind == "KEY_MISSING":
            resolved = diff.target in actual_keys
            return VerificationResult(
                difference_id=diff.id,
                resolved=resolved,
                evidence={
                    "key": diff.target,
                    "found_in_redis": resolved,
                    "all_keys": sorted(actual_keys),
                },
            )

        # UNDECLARED_KEY — not auto-fixable, trust execution result
        return VerificationResult(
            difference_id=diff.id,
            resolved=False,
            evidence={"kind": diff.kind, "requires_manual_review": True},
        )
