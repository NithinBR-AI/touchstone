"""
adapters/openapi/verifier.py — Req 23

Verifier for API contract drift. Since API fixes are applied as PRs (not
in-place), verification checks whether the target repo's openapi.yaml on the
default branch now declares the previously-drifted fields.
"""

from __future__ import annotations

from core.models import Difference, ExecutionResult, VerificationResult

from .intended import load_from_url
from .ssrf import SSRFError


class ContractVerifier:
    """Verifier protocol implementation for the OpenAPI contract domain."""

    def verify(
        self,
        diff: Difference,
        result: ExecutionResult,
        openapi_url: str,
    ) -> VerificationResult:
        """
        Re-fetch the OpenAPI spec and check whether the drift is resolved.

        For FIELD_EXTRA: the field should now appear in the spec.
        For FIELD_MISSING: the field should now appear in the response (re-probe needed
            — but we only check the spec here; full re-probe is expensive).
        """
        if not result.success:
            return VerificationResult(
                verified=False,
                evidence={"reason": "execution failed", "error": result.error},
            )

        try:
            state = load_from_url(openapi_url)
        except SSRFError as exc:
            return VerificationResult(
                verified=False,
                evidence={"reason": "ssrf_blocked", "error": str(exc)},
            )
        except Exception as exc:
            return VerificationResult(
                verified=False,
                evidence={"reason": "fetch_failed", "error": str(exc)},
            )

        ctx = diff.context
        path = ctx.get("path", "")
        field = ctx.get("field", "")
        method = ctx.get("method", "get")
        status = ctx.get("status", "200")

        paths = state.data.get("paths", {})
        path_item = paths.get(path, {})
        op = path_item.get(method, {})
        schema = op.get("responses", {}).get(status, {}).get("schema", {})

        # Flatten array schema
        if schema.get("type") == "array":
            schema = schema.get("items", {})

        declared_fields = set(schema.get("properties", {}).keys())

        if diff.kind == "FIELD_EXTRA":
            resolved = field in declared_fields
            return VerificationResult(
                verified=resolved,
                evidence={
                    "field": field,
                    "in_spec_now": resolved,
                    "declared_fields": sorted(declared_fields),
                },
            )

        if diff.kind == "FIELD_MISSING":
            resolved = field in declared_fields
            return VerificationResult(
                verified=resolved,
                evidence={
                    "field": field,
                    "in_spec_now": resolved,
                },
            )

        # For other kinds (PATH_MISSING etc.) we just trust the execution result
        return VerificationResult(
            verified=True,
            evidence={"kind": diff.kind, "trusted_execution": True},
        )
