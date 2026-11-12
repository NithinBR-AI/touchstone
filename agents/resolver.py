"""
agents/resolver.py — Task 2.12, Req 15

Week 1: MockResolver. No LLM, no network. A deterministic canned responder
stands in for the model. Everything around the responder (prompt building,
prompt SHA-256, JSON schema validation, one retry, review-only fallback) is the
real logic and will be reused when the Bedrock responder replaces the mock in
week 4 (task 7.1).

Rules:
  · Only the Difference's domain/kind/target/context reach the prompt.
  · safe_fix=False or DECLARED_VERSION_MISMATCH -> review-only, responder not called.
  · Invalid responder output -> retry once -> still invalid -> review-only
    (the orchestrator then marks the ghost BLOCKED).
  · The resolver proposes; core/validator.py decides. Nothing here is trusted.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from agents.prompts import build_prompt, prompt_sha256
from core.models import Difference, Resolution, SQLChange

Responder = Callable[[str, Difference], Any]

ALLOWED_ACTIONS = {"sql", "review"}
ALLOWED_OPERATIONS = {"CREATE_INDEX", "ALTER_COLUMN_TYPE"}
REVIEW_ONLY_KINDS = {"DECLARED_VERSION_MISMATCH"}
MAX_ATTEMPTS = 2  # one live call + one schema-validation retry

_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


def validate_payload(payload: Any) -> str:
    """Return "" if payload matches the resolver output schema, else a reason."""
    if not isinstance(payload, dict):
        return "payload is not an object"
    for key in ("action", "statement", "operation", "rationale"):
        if key not in payload:
            return f"missing key '{key}'"
    if payload["action"] not in ALLOWED_ACTIONS:
        return "invalid action"
    if not isinstance(payload["rationale"], str):
        return "rationale must be a string"
    if payload["action"] == "sql":
        if (
            not isinstance(payload["statement"], str)
            or not payload["statement"].strip()
        ):
            return "sql action requires a statement"
        if payload["operation"] not in ALLOWED_OPERATIONS:
            return "invalid operation"
    else:
        if payload["statement"] is not None or payload["operation"] is not None:
            return "review action must have null statement and operation"
    return ""


# ---------------------------------------------------------------------------
# Canned responder (week-1 stand-in for the model)
# ---------------------------------------------------------------------------


def _review(rationale: str) -> dict[str, Any]:
    return {
        "action": "review",
        "statement": None,
        "operation": None,
        "rationale": rationale,
    }


def canned_responder(prompt: str, difference: Difference) -> dict[str, Any]:
    ctx = difference.context

    if difference.kind == "INDEX_MISSING":
        table = ctx.get("table", "")
        cols = list(ctx.get("columns", []))
        if not cols or not all(
            _PLAIN_IDENT.match(x) for x in [table, *cols, difference.target]
        ):
            return _review(
                "Identifiers need quoting; not generating SQL automatically."
            )
        return {
            "action": "sql",
            "statement": f"CREATE INDEX {difference.target} ON {table} ({', '.join(cols)})",
            "operation": "CREATE_INDEX",
            "rationale": f"Declared index on {table}({', '.join(cols)}) is missing.",
        }

    if difference.kind == "TYPE_CHANGED":
        table, col = ctx.get("table", ""), ctx.get("column", "")
        new_type = str(ctx.get("intended_type", ""))
        if not (_PLAIN_IDENT.match(table) and _PLAIN_IDENT.match(col)):
            return _review(
                "Identifiers need quoting; not generating SQL automatically."
            )
        if not re.fullmatch(r"[a-z ]+(\(\d+\))?", new_type):
            return _review("Intended type is not a simple widening target.")
        return {
            "action": "sql",
            "statement": f"ALTER TABLE {table} ALTER COLUMN {col} TYPE {new_type}",
            "operation": "ALTER_COLUMN_TYPE",
            "rationale": (
                f"Widen {table}.{col} from {ctx.get('original_actual_type')} to {new_type}."
            ),
        }

    return _review(f"No automatic fix for {difference.kind}.")


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class MockResolver:
    """Resolver protocol implementation. Pass a different responder to test failures."""

    def __init__(self, responder: Responder | None = None) -> None:
        self._responder = responder or canned_responder

    def resolve(self, difference: Difference) -> Resolution:
        prompt = build_prompt(difference)
        digest = prompt_sha256(prompt)

        if (
            not difference.context.get("safe_fix", False)
            or difference.kind in REVIEW_ONLY_KINDS
        ):
            return self._resolution(
                difference, digest, None, "Review required: not auto-fixable."
            )

        last_error = ""
        for _ in range(MAX_ATTEMPTS):
            payload = self._responder(prompt, difference)
            last_error = validate_payload(payload)
            if last_error:
                continue
            if payload["action"] == "review":
                return self._resolution(difference, digest, None, payload["rationale"])
            change = SQLChange(
                statement=payload["statement"].strip(),
                operation=payload["operation"],
                target=difference.target,
            )
            return self._resolution(difference, digest, change, payload["rationale"])

        return self._resolution(
            difference,
            digest,
            None,
            f"Blocked: resolver output failed schema validation twice ({last_error}).",
        )

    @staticmethod
    def _resolution(
        difference: Difference,
        digest: str,
        change: SQLChange | None,
        rationale: str,
    ) -> Resolution:
        return Resolution(
            difference_id=difference.id,
            difference_signature=difference.signature,
            proposed_change=change,
            rationale=rationale,
            prompt_sha256=digest,
        )
