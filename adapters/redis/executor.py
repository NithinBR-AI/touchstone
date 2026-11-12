"""
adapters/redis/executor.py

Executor for cache drift. Only KEY_MISSING is auto-fixable.

For KEY_MISSING: calls POST /cache/warm?key=<key> on the target app to
populate the declared key from the app's own DB. Safe — only sets keys
declared in cache_config.yaml.

For UNDECLARED_KEY: BLOCKED — requires manual review and deletion.
No automatic deletion of Redis keys.
"""

from __future__ import annotations

import urllib.parse

from core.models import Difference, ExecutionResult

from .actual import _CLIENT


class CacheExecutor:
    """Executor protocol implementation for the Redis cache domain."""

    def __init__(self, cache_warm_url: str) -> None:
        self.cache_warm_url = cache_warm_url

    def execute(self, diff: Difference) -> ExecutionResult:
        if diff.kind != "KEY_MISSING":
            return ExecutionResult(
                difference_id=diff.id,
                success=False,
                error_code="NOT_AUTO_FIXABLE",
                error_class="PolicyError",
                error_summary=f"kind={diff.kind} requires manual review; no automated fix",
            )

        url = f"{self.cache_warm_url}?key={urllib.parse.quote(diff.target, safe='')}"
        try:
            resp = _CLIENT.post(url, timeout=30.0)
            resp.raise_for_status()
            return ExecutionResult(
                difference_id=diff.id,
                success=True,
                applied_change=None,
                error_code="",
                error_class="",
                error_summary="",
            )
        except Exception as exc:
            code = getattr(
                getattr(exc, "response", None), "status_code", "CONNECTION_ERROR"
            )
            return ExecutionResult(
                difference_id=diff.id,
                success=False,
                error_code=str(code),
                error_class=type(exc).__name__,
                error_summary=f"warm endpoint failed: {type(exc).__name__}",
            )

    def rollback(self, diff: Difference, result: ExecutionResult) -> None:
        """Cache domain does not support rollback."""
        raise NotImplementedError("rollback not supported for cache domain")
