"""
adapters/redis/verifier.py

Verifier for cache drift. Re-fetches /cache/status and checks whether
the drift is resolved.

For KEY_MISSING: key should now appear in Redis.
For UNDECLARED_KEY: not auto-fixable, verification is a no-op.

Signature follows core.interfaces.Verifier: verify(target, difference).
"""

from __future__ import annotations

from core.models import ComparisonTarget, Difference, VerificationResult

from .actual import load_from_url as load_actual


class CacheVerifier:
    """Verifier protocol implementation for the Redis cache domain."""

    def __init__(self, cache_status_url: str) -> None:
        self.cache_status_url = cache_status_url

    def verify(
        self,
        target: ComparisonTarget,
        difference: Difference,
    ) -> VerificationResult:
        try:
            actual_state = load_actual(self.cache_status_url)
        except Exception as exc:
            return VerificationResult(
                difference_id=difference.id,
                resolved=False,
                evidence={"reason": "fetch_failed", "error": type(exc).__name__},
            )

        actual_keys: set[str] = set(actual_state.data.get("keys", []))

        if difference.kind == "KEY_MISSING":
            resolved = difference.target in actual_keys
            return VerificationResult(
                difference_id=difference.id,
                resolved=resolved,
                evidence={
                    "key": difference.target,
                    "found_in_redis": resolved,
                    "all_keys": sorted(actual_keys),
                },
            )

        # UNDECLARED_KEY — not auto-fixable, requires manual review
        return VerificationResult(
            difference_id=difference.id,
            resolved=False,
            evidence={"kind": difference.kind, "requires_manual_review": True},
        )
