"""
adapters/redis/executor.py

Executor for cache drift. Only KEY_MISSING is auto-fixable.

For KEY_MISSING: calls POST /cache/warm?key=<key> on the target app to
populate the declared key from the app's own DB. Safe — only sets keys
declared in cache_config.yaml.

For UNDECLARED_KEY: calls DELETE /cache/delete?key=<key> on the target app
to remove the undeclared key from Redis.

The resolver always produces a populated ChangeProposal for cache diffs
(via _cache_proposal). proposed_change=None strictly means no fix is
available and the diff becomes REVIEW_REQUIRED — it never reaches this
executor. The executor reads resolution.proposed_change for the applied
change record.
"""

from __future__ import annotations

import urllib.parse

from core.models import ChangeProposal, Difference, ExecutionResult, Resolution

from .actual import _CLIENT


class CacheExecutor:
    """Executor protocol implementation for the Redis cache domain."""

    def __init__(self, cache_warm_url: str) -> None:
        self.cache_warm_url = cache_warm_url
        self.cache_delete_url = cache_warm_url.replace("/cache/warm", "/cache/delete")

    def execute(
        self, resolution: Resolution, difference: Difference
    ) -> ExecutionResult:  # noqa: ARG002
        if difference.kind == "UNDECLARED_KEY":
            return self._delete_key(difference)
        if difference.kind != "KEY_MISSING":
            return ExecutionResult(
                difference_id=difference.id,
                success=False,
                error_code="NOT_AUTO_FIXABLE",
                error_class="PolicyError",
                error_summary=f"kind={difference.kind} requires manual review; no automated fix",
            )

        key = difference.target
        url = f"{self.cache_warm_url}?key={urllib.parse.quote(key, safe='')}"
        try:
            resp = _CLIENT.post(url, timeout=30.0)
            resp.raise_for_status()
            applied = resolution.proposed_change
            return ExecutionResult(
                difference_id=difference.id,
                success=True,
                applied_change=applied,
                error_code="",
                error_class="",
                error_summary="",
            )
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", "CONNECTION_ERROR")
            return ExecutionResult(
                difference_id=difference.id,
                success=False,
                error_code=str(code),
                error_class=type(exc).__name__,
                error_summary=f"warm endpoint failed: {type(exc).__name__}",
            )

    def _delete_key(self, difference: Difference) -> ExecutionResult:
        key = difference.target
        url = f"{self.cache_delete_url}?key={urllib.parse.quote(key, safe='')}"
        try:
            resp = _CLIENT.delete(url, timeout=30.0)
            resp.raise_for_status()
            return ExecutionResult(
                difference_id=difference.id,
                success=True,
                applied_change=None,
                error_code="",
                error_class="",
                error_summary="",
            )
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", "CONNECTION_ERROR")
            return ExecutionResult(
                difference_id=difference.id,
                success=False,
                error_code=str(code),
                error_class=type(exc).__name__,
                error_summary=f"delete endpoint failed: {type(exc).__name__}",
            )

    def rollback(self, result: ExecutionResult) -> bool:
        """Cache domain does not support rollback."""
        raise NotImplementedError("rollback not supported for cache domain")
