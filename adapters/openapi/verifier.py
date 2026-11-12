"""
adapters/openapi/verifier.py — Req 23

Verifier for API contract drift. Since API fixes are applied as PRs (not
in-place), verification checks whether the target repo's openapi.yaml on the
default branch now declares the previously-drifted fields.
"""

from __future__ import annotations

from core.models import ComparisonTarget, Difference, VerificationResult

from .intended import load_from_url
from .ssrf import SSRFError


class ContractVerifier:
    """Verifier protocol implementation for the OpenAPI contract domain."""

    def verify(
        self,
        target: ComparisonTarget,
        difference: Difference,
    ) -> VerificationResult:
        """
        Re-fetch the OpenAPI spec and check whether the drift is resolved.

        openapi_url is taken from target.context["openapi_url"].

        For FIELD_EXTRA: the field should now appear in the spec.
        For FIELD_MISSING: the field should now appear in the response (re-probe needed
            — but we only check the spec here; full re-probe is expensive).
        """
        openapi_url = (target.context or {}).get("openapi_url", "")
        if not openapi_url:
            return VerificationResult(
                difference_id=difference.id,
                resolved=True,
                evidence={"reason": "no openapi_url in target context — trusting execution"},
            )

        try:
            state = load_from_url(openapi_url)
        except SSRFError as exc:
            return VerificationResult(
                difference_id=difference.id,
                resolved=False,
                evidence={"reason": "ssrf_blocked", "error": str(exc)},
            )
        except Exception as exc:
            return VerificationResult(
                difference_id=difference.id,
                resolved=False,
                evidence={"reason": "fetch_failed", "error": str(exc)},
            )

        ctx = difference.context
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

        if difference.kind == "FIELD_EXTRA":
            is_resolved = field in declared_fields
            return VerificationResult(
                difference_id=difference.id,
                resolved=is_resolved,
                evidence={
                    "field": field,
                    "in_spec_now": is_resolved,
                    "declared_fields": sorted(declared_fields),
                },
            )

        if difference.kind == "FIELD_MISSING":
            is_resolved = field in declared_fields
            return VerificationResult(
                difference_id=difference.id,
                resolved=is_resolved,
                evidence={
                    "field": field,
                    "in_spec_now": is_resolved,
                },
            )

        # For other kinds (PATH_MISSING etc.) we just trust the execution result
        return VerificationResult(
            difference_id=difference.id,
            resolved=True,
            evidence={"kind": difference.kind, "trusted_execution": True},
        )
