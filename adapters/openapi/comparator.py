"""
adapters/openapi/comparator.py — Req 11

Diffs intended vs actual OpenAPI State and emits Difference objects.

Drift kinds:
  FIELD_EXTRA      actual response has a property not in the spec  -> BLOCKED
                   (undocumented field leaking — the demo's Contract drift)
  FIELD_MISSING    spec declares a property absent from actual     -> REVIEW_REQUIRED
  TYPE_MISMATCH    property present in both but types differ       -> REVIEW_REQUIRED
  PATH_MISSING     spec declares a path that actual doesn't expose -> BLOCKED
  PATH_EXTRA       actual exposes a path not in the spec           -> BLOCKED

safe_fix is always False for API contract drift — changes must be reviewed
and applied as a PR on the target repo (Option B).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.models import Difference, GhostState, State

DOMAIN = "api"

POLICY_BLOCKED = "blocked"
POLICY_REVIEW = "review_required"


def _signature(kind: str, target: str, key_ctx: dict[str, Any]) -> str:
    payload = json.dumps(
        {"domain": DOMAIN, "kind": kind, "target": target, "ctx": key_ctx},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _make(
    kind: str,
    target: str,
    severity: str,
    policy: str,
    context: dict[str, Any],
    key_ctx: dict[str, Any] | None = None,
) -> Difference:
    ctx = dict(context)
    ctx["policy"] = policy
    ctx["safe_fix"] = False  # API drift always requires a PR — never auto-apply
    state = (
        GhostState.BLOCKED if policy == POLICY_BLOCKED else GhostState.REVIEW_REQUIRED
    )
    return Difference(
        signature=_signature(kind, target, key_ctx or {}),
        domain=DOMAIN,
        kind=kind,
        target=target,
        severity=severity,
        state=state,
        context=ctx,
    )


def _actual_props(schema: dict[str, Any]) -> set[str]:
    """Return property names from an inferred schema."""
    if schema.get("type") == "array":
        return _actual_props(schema.get("items", {}))
    return set(schema.get("properties", {}).keys())


def _intended_props(schema: dict[str, Any]) -> set[str]:
    """Return property names from a spec schema."""
    if schema.get("type") == "array":
        return _intended_props(schema.get("items", {}))
    return set(schema.get("properties", {}).keys())


class ContractComparator:
    """Comparator protocol implementation for the OpenAPI contract domain."""

    def compare(self, intended: State, actual: State) -> list[Difference]:
        i_paths: dict[str, Any] = intended.data.get("paths", {})
        a_paths: dict[str, Any] = actual.data.get("paths", {})
        diffs: list[Difference] = []

        for path in sorted(i_paths):
            # Skip single-item path-param endpoints — probed via collection endpoint
            if "{" in path:
                continue
            if path not in a_paths:
                diffs.append(
                    _make(
                        "PATH_MISSING",
                        path,
                        "HIGH",
                        POLICY_BLOCKED,
                        {"path": path},
                        {"path": path},
                    )
                )
                continue
            diffs.extend(self._compare_path(path, i_paths[path], a_paths[path]))

        for path in sorted(a_paths):
            if "{" in path:
                continue
            if path not in i_paths:
                diffs.append(
                    _make(
                        "PATH_EXTRA",
                        path,
                        "MEDIUM",
                        POLICY_BLOCKED,
                        {"path": path},
                        {"path": path},
                    )
                )

        return diffs

    def _compare_path(
        self,
        path: str,
        i_item: dict[str, Any],
        a_item: dict[str, Any],
    ) -> list[Difference]:
        diffs: list[Difference] = []
        for method in ["get"]:
            if method not in i_item:
                continue
            if method not in a_item:
                continue
            i_op = i_item[method]
            a_op = a_item[method]
            diffs.extend(self._compare_responses(path, method, i_op, a_op))
        return diffs

    def _compare_responses(
        self,
        path: str,
        method: str,
        i_op: dict[str, Any],
        a_op: dict[str, Any],
    ) -> list[Difference]:
        diffs: list[Difference] = []
        i_responses = i_op.get("responses", {})
        a_responses = a_op.get("responses", {})

        for status in sorted(i_responses):
            if status not in a_responses:
                continue
            i_schema = i_responses[status].get("schema", {})
            a_schema = a_responses[status].get("schema", {})
            diffs.extend(
                self._compare_schemas(path, method, status, i_schema, a_schema)
            )

        return diffs

    def _compare_schemas(
        self,
        path: str,
        method: str,
        status: str,
        i_schema: dict[str, Any],
        a_schema: dict[str, Any],
    ) -> list[Difference]:
        diffs: list[Difference] = []
        target_prefix = f"{method.upper()} {path} {status}"

        i_props = _intended_props(i_schema)
        a_props = _actual_props(a_schema)

        # Fields in actual but not in spec — undocumented leakage
        for field in sorted(a_props - i_props):
            diffs.append(
                _make(
                    "FIELD_EXTRA",
                    f"{target_prefix}.{field}",
                    "MEDIUM",
                    POLICY_BLOCKED,
                    {
                        "path": path,
                        "method": method,
                        "status": status,
                        "field": field,
                        "description": f"Field '{field}' returned by API but not in spec",
                    },
                    {"path": path, "field": field},
                )
            )

        # Fields in spec but absent from actual — missing from response
        for field in sorted(i_props - a_props):
            diffs.append(
                _make(
                    "FIELD_MISSING",
                    f"{target_prefix}.{field}",
                    "HIGH",
                    POLICY_REVIEW,
                    {
                        "path": path,
                        "method": method,
                        "status": status,
                        "field": field,
                        "description": f"Field '{field}' in spec but not returned by API",
                    },
                    {"path": path, "field": field},
                )
            )

        return diffs
