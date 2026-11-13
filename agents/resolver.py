"""
agents/resolver.py

Resolver implementations. The resolver proposes one fix per Difference.

MockResolver: deterministic canned responder — no LLM, no network.
MantleResolver: production resolver backed by Mantle (Bedrock-backed LLM).

Both return Resolution with proposed_change as a ChangeProposal (or None).
Core never inspects ChangeProposal.payload contents — that is adapter knowledge.

Domain routing (MantleResolver):
  database → ultra() → QWEN 235B — SQL fix proposals
  api      → deepseek_pro() — file patch proposals; LLM called at resolve time
             so validated content equals executed content (safety requirement).
  cache    → deterministic ChangeProposal built from Difference — no LLM.
  other    → ultra() as default

App 2 (Contract Observer) note:
  The LLM is called here at resolve time, not at PR creation time.
  The executor receives the validated patched_content from the ChangeProposal
  payload — no second LLM call in execute_batch(). This ensures what is
  validated and authorized equals what is committed to GitHub.
  If the GitHub file fetch or LLM call fails at resolve time, proposed_change
  is None — the diff becomes REVIEW_REQUIRED, never reaching execution.

Rules:
  · Only the Difference's domain/kind/target/context reach the prompt.
  · REVIEW_ONLY_KINDS → review-only, responder not called.
  · Invalid responder output → retry once → still invalid → review-only.
  · The resolver proposes; adapters/postgres/validator.py decides for SQL.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

from agents.prompts import build_api_batch_user_message, build_prompt, prompt_sha256
from core.models import ChangeProposal, Difference, Resolution

logger = logging.getLogger(__name__)

Responder = Callable[[str, Difference], Any]

ALLOWED_ACTIONS = {"sql", "review", "patch"}
ALLOWED_OPERATIONS = {
    "CREATE_INDEX",
    "ALTER_COLUMN_TYPE",
    "DROP_INDEX",
    "DROP_COLUMN",
    "ADD_COLUMN",
    "CREATE_TABLE",
    "DROP_TABLE",
}
REVIEW_ONLY_KINDS = {"DECLARED_VERSION_MISMATCH"}
MAX_ATTEMPTS = 2

_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# Which file to fetch per drift kind (api domain)
_KIND_TO_FILE: dict[str, str] = {
    "FIELD_EXTRA": "openapi.yaml",
    "FIELD_UNDOCUMENTED": "openapi.yaml",
    "FIELD_MISSING": "main.py",
}


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


def validate_payload(payload: Any) -> str:
    """Return "" if payload matches the resolver output schema, else a reason."""
    if not isinstance(payload, dict):
        return "payload is not an object"
    if "action" not in payload:
        return "missing key 'action'"
    if "rationale" not in payload:
        return "missing key 'rationale'"
    if payload["action"] not in ALLOWED_ACTIONS:
        return "invalid action"
    if not isinstance(payload["rationale"], str):
        return "rationale must be a string"
    if payload["action"] == "sql":
        for key in ("statement", "operation"):
            if key not in payload:
                return f"missing key '{key}'"
        if not isinstance(payload["statement"], str) or not payload["statement"].strip():
            return "sql action requires a statement"
        if payload["operation"] not in ALLOWED_OPERATIONS:
            return "invalid operation"
    elif payload["action"] == "patch":
        if "patched_content" not in payload:
            return "missing key 'patched_content'"
        if (
            not isinstance(payload["patched_content"], str)
            or not payload["patched_content"].strip()
        ):
            return "patch action requires patched_content"
    else:  # review
        if payload.get("statement") is not None or payload.get("operation") is not None:
            return "review action must have null statement and operation"
    return ""


# ---------------------------------------------------------------------------
# Canned responder (week-1 stand-in, used by MockResolver)
# ---------------------------------------------------------------------------


def _review(rationale: str) -> dict[str, Any]:
    return {"action": "review", "statement": None, "operation": None, "rationale": rationale}


def canned_responder(prompt: str, difference: Difference) -> dict[str, Any]:
    ctx = difference.context
    if difference.kind == "INDEX_MISSING":
        table = ctx.get("table", "")
        cols = list(ctx.get("columns", []))
        if not cols or not all(_PLAIN_IDENT.match(x) for x in [table, *cols, difference.target]):
            return _review("Identifiers need quoting; not generating SQL automatically.")
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
            return _review("Identifiers need quoting; not generating SQL automatically.")
        if not re.fullmatch(r"[a-z ]+(\(\d+\))?", new_type):
            return _review("Intended type is not a simple widening target.")
        return {
            "action": "sql",
            "statement": f"ALTER TABLE {table} ALTER COLUMN {col} TYPE {new_type}",
            "operation": "ALTER_COLUMN_TYPE",
            "rationale": f"Widen {table}.{col} from {ctx.get('original_actual_type')} to {new_type}.",
        }
    return _review(f"No automatic fix for {difference.kind}.")


# ---------------------------------------------------------------------------
# ChangeProposal builders
# ---------------------------------------------------------------------------


def _sql_proposal(difference: Difference, payload: dict[str, Any]) -> ChangeProposal:
    return ChangeProposal(
        domain=difference.domain,
        target=difference.target,
        operation=payload["operation"],
        payload={
            "statement": payload["statement"].strip(),
            "operation": payload["operation"],
        },
    )


def _patch_proposal(
    difference: Difference,
    patched_content: str,
    file_path: str,
    validated: bool = True,
) -> ChangeProposal:
    return ChangeProposal(
        domain=difference.domain,
        target=difference.target,
        operation="FILE_PATCH",
        payload={
            "patched_content": patched_content,
            "file_path": file_path,
            "validated": validated,
        },
    )


def _cache_proposal(difference: Difference) -> ChangeProposal:
    """Deterministic cache proposal — no LLM needed."""
    op = "CACHE_WARM" if difference.kind == "KEY_MISSING" else "CACHE_DELETE"
    return ChangeProposal(
        domain=difference.domain,
        target=difference.target,
        operation=op,
        payload={"key": difference.target, "kind": difference.kind},
    )


# ---------------------------------------------------------------------------
# MockResolver
# ---------------------------------------------------------------------------


class MockResolver:
    """Resolver protocol implementation. Deterministic — no LLM, no network."""

    def __init__(self, responder: Responder | None = None) -> None:
        self._responder = responder or canned_responder

    def resolve(self, difference: Difference) -> Resolution:
        prompt = build_prompt(difference)
        digest = prompt_sha256(prompt)

        if not difference.context.get("safe_fix", False) or difference.kind in REVIEW_ONLY_KINDS:
            return self._resolution(difference, digest, None, "Review required: not auto-fixable.")

        # Cache domain — deterministic proposal
        if difference.domain == "cache":
            proposal = _cache_proposal(difference)
            return self._resolution(
                difference,
                digest,
                proposal,
                f"Cache {proposal.operation} for key {difference.target}",
            )

        last_error = ""
        for _ in range(MAX_ATTEMPTS):
            payload = self._responder(prompt, difference)
            last_error = validate_payload(payload)
            if last_error:
                continue
            if payload["action"] == "review":
                return self._resolution(difference, digest, None, payload["rationale"])
            proposal = _sql_proposal(difference, payload)
            return self._resolution(difference, digest, proposal, payload["rationale"])

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
        proposal: ChangeProposal | None,
        rationale: str,
    ) -> Resolution:
        return Resolution(
            difference_id=difference.id,
            difference_signature=difference.signature,
            proposed_change=proposal,
            rationale=rationale,
            prompt_sha256=digest,
        )


# ---------------------------------------------------------------------------
# MantleResolver
# ---------------------------------------------------------------------------


class MantleResolver:
    """
    Production resolver backed by Mantle (Bedrock-backed OpenAI-compatible API).

    Domain routing:
      database → ultra() → QWEN 235B  (SQL fix proposals)
      api      → deepseek_pro()        (file patch proposals, called at resolve time)
      cache    → deterministic ChangeProposal (no LLM)
      other    → ultra() as default

    If MANTLE_API_KEY is not set, falls back to MockResolver behaviour.

    App 2 safety note:
      LLM is called here at resolve time. GitHub file is fetched here.
      If either fails, proposed_change=None → diff becomes REVIEW_REQUIRED.
      The executor reads patched_content from the validated proposal payload —
      no second LLM call. What is validated equals what is committed.
    """

    def __init__(self) -> None:
        self._fallback = MockResolver()

    def resolve(self, difference: Difference) -> Resolution:
        if not os.environ.get("MANTLE_API_KEY"):
            logger.warning("MANTLE_API_KEY not set — falling back to MockResolver")
            return self._fallback.resolve(difference)

        prompt = build_prompt(difference)
        digest = prompt_sha256(prompt)

        # Cache domain — deterministic, no LLM
        if difference.domain == "cache":
            proposal = _cache_proposal(difference)
            return self._make_resolution(
                difference,
                digest,
                proposal,
                f"Cache {proposal.operation} for key {difference.target}",
            )

        # API domain — fetch file from GitHub and call LLM at resolve time
        if difference.domain == "api":
            return self._resolve_api(difference, digest)

        if difference.kind in REVIEW_ONLY_KINDS:
            return self._make_resolution(
                difference,
                digest,
                None,
                "Review required: version mismatch requires manual investigation.",
            )

        # Database domain — SQL fix via LLM
        last_error = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                raw = self._call_llm_db(prompt)
                payload = json.loads(raw)
            except (json.JSONDecodeError, Exception) as exc:
                last_error = f"LLM call/parse error: {type(exc).__name__}"
                logger.warning("Attempt %d/%d failed: %s", attempt + 1, MAX_ATTEMPTS, last_error)
                continue

            last_error = validate_payload(payload)
            if last_error:
                logger.warning(
                    "Attempt %d/%d invalid payload: %s", attempt + 1, MAX_ATTEMPTS, last_error
                )
                continue

            if payload["action"] == "review":
                return self._make_resolution(difference, digest, None, payload["rationale"])

            proposal = _sql_proposal(difference, payload)
            return self._make_resolution(difference, digest, proposal, payload["rationale"])

        return self._make_resolution(
            difference,
            digest,
            None,
            f"Blocked: resolver output failed schema validation after {MAX_ATTEMPTS} attempts ({last_error}).",
        )

    def _resolve_api(self, difference: Difference, digest: str) -> Resolution:
        """
        Build a lightweight proposal with field metadata — no LLM, no GitHub fetch.
        LLM patch generation and validation happens at execute time (Exorcise).
        """
        kind = difference.kind
        file_name = _KIND_TO_FILE.get(kind)
        if not file_name:
            return self._make_resolution(
                difference,
                digest,
                None,
                f"API kind '{kind}' has no automated fix — requires manual review.",
            )

        fields = difference.context.get("fields") or (
            [difference.context["field"]]
            if difference.context.get("field")
            else [difference.target]
        )
        path = difference.context.get("path", "")
        schema_name = difference.context.get("schema_name", "")
        operation_id = difference.context.get("operation_id", "")

        proposal = ChangeProposal(
            domain=difference.domain,
            target=difference.target,
            operation="FILE_PATCH",
            payload={
                "kind": kind,
                "fields": fields,
                "path": path,
                "file_name": file_name,
                "schema_name": schema_name,
                "operation_id": operation_id,
            },
        )
        return self._make_resolution(
            difference,
            digest,
            proposal,
            f"Drift detected: {kind} on {file_name} — patch will be generated at execute time.",
        )

    def _call_llm_db(self, prompt: str) -> str:
        from agents.mantle_client import strip_llm_wrapper, ultra

        raw = ultra(system=prompt, user="Propose the fix as a JSON object.")
        return strip_llm_wrapper(raw)

    @staticmethod
    def _make_resolution(
        difference: Difference,
        digest: str,
        proposal: ChangeProposal | None,
        rationale: str,
    ) -> Resolution:
        return Resolution(
            difference_id=difference.id,
            difference_signature=difference.signature,
            proposed_change=proposal,
            rationale=rationale,
            prompt_sha256=digest,
        )
