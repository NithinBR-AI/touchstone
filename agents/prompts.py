"""
agents/prompts.py — Req 15

Builds domain-aware resolver prompts. Only the Difference's domain, kind,
target, and context are included. The per-run UUID is excluded so the same
ghost always produces the same prompt and the same prompt SHA-256.

Domain routing:
  database → SQL prompt  (expects JSON with action/statement/operation/rationale)
  api      → API prompt  (expects JSON with action="review" and rationale only)
  cache    → cache prompt (expects JSON with action="review" and rationale only)
  other    we wait w→ generic prompt (review-only)
"""

from __future__ import annotations

import hashlib
import json

from agents.redaction import redact_value
from core.models import Difference

# Which file to fetch per drift kind
_KIND_TO_FILE: dict[str, str] = {
    "FIELD_EXTRA": "openapi.yaml",
    "FIELD_MISSING": "main.py",
}

PROMPT_VERSION = "resolver-v2"

# ---------------------------------------------------------------------------
# Database domain — SQL fix proposals
# ---------------------------------------------------------------------------

_DB_SYSTEM_PROMPT = """\
You are Touchstone, an automated database schema drift resolver.
Your job is to propose exactly one SQL DDL fix for the drift described below.

You MUST respond with a single JSON object and nothing else — no markdown, no prose:
{
  "action": "sql" | "review",
  "statement": "<exactly one SQL DDL statement>" | null,
  "operation": "<DDL operation name matching the statement, e.g. CREATE_INDEX, DROP_INDEX, ALTER_COLUMN_TYPE, ADD_COLUMN, DROP_COLUMN>" | null,
  "rationale": "<one or two sentences explaining the fix>"
}

Rules:
- Always use action="sql" and propose a fix. Use action="review" ONLY if identifiers are unsafe to use unquoted.
- Never emit DELETE, TRUNCATE, SET, GRANT, or more than one statement.
- Never add a USING clause to ALTER COLUMN TYPE.
- Never quote identifiers unless they contain uppercase, spaces, or reserved words.
- operation must exactly match the DDL kind: CREATE_INDEX, DROP_INDEX, ALTER_COLUMN_TYPE, ADD_COLUMN, DROP_COLUMN, CREATE_TABLE, DROP_TABLE.
"""

# ---------------------------------------------------------------------------
# API domain — LLM generates the actual patched file content
# ---------------------------------------------------------------------------

_API_SYSTEM_PROMPT = """\
You are Touchstone, an automated API contract drift fixer.
The drift described below has been detected between the OpenAPI specification
and the live API's actual response payload.

You will be given the CURRENT FILE CONTENT that needs to be patched.
Your job is to return the COMPLETE patched file — every line, nothing omitted.

You MUST respond with a single JSON object and nothing else — no markdown, no prose:
{
  "action": "patch",
  "patched_content": "<the complete updated file content as a string>",
  "rationale": "<one or two sentences: what drifted and what the patch does>"
}

Drift kinds you may encounter:
- FIELD_EXTRA / FIELD_UNDOCUMENTED: Fields are present in the live API response but
  absent from the OpenAPI spec. Add ALL of them to the correct schema's properties
  block in openapi.yaml.
- FIELD_MISSING: Fields are declared in the OpenAPI spec but absent from the live API
  response. Add ALL of them to the return dict of the correct endpoint function in
  main.py, each with a None placeholder value and a # TODO: implement comment.

Rules:
- Fix ALL fields listed in the task — never partial fixes.
- patched_content must be the COMPLETE file — not a diff, not a snippet.
- The output file must produce a valid git diff against the input where every changed
  line is intentional and directly related to fixing the drift described.
  Any other difference is a bug in your output.
"""

# ---------------------------------------------------------------------------
# Cache domain — cache drift rationale (executor handles the actual fix)
# ---------------------------------------------------------------------------

_CACHE_SYSTEM_PROMPT = """\
You are Touchstone, an automated cache configuration drift analyst.
The drift described below has been detected between the declared cache
configuration (in cache_config.yaml) and the live cache state.

The fix will be applied automatically by the executor where safe.
Your role is to provide a clear, concise rationale that will be recorded in the
audit log and surfaced to the operator.

You MUST respond with a single JSON object and nothing else — no markdown, no prose:
{
  "action": "review",
  "statement": null,
  "operation": null,
  "rationale": "<two to four sentences: what drifted, what the fix does, and any risk>"
}

Drift kinds you may encounter:
- TTL_DRIFT:       The live cache TTL differs from the declared TTL.
- KEY_MISSING:     A key declared in the config is absent from the live cache.
- KEY_EXTRA:       A key exists in the live cache but is not declared in the config.
- EVICTION_POLICY_MISMATCH: The eviction policy on the live cache differs from declared.
- MAX_MEMORY_DRIFT: The live cache's maxmemory setting differs from declared.

Provide a rationale grounded in the difference context. Be specific about the key,
TTL value, or policy name affected, and explain the operational risk (e.g. premature
eviction, stale data, unbounded growth).
"""

# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------

_GENERIC_SYSTEM_PROMPT = """\
You are Touchstone, an automated infrastructure drift analyst.
The drift described below has been detected.

You MUST respond with a single JSON object and nothing else:
{
  "action": "review",
  "statement": null,
  "operation": null,
  "rationale": "<two to four sentences describing the drift and recommended remediation>"
}
"""

_DOMAIN_PROMPTS: dict[str, str] = {
    "database": _DB_SYSTEM_PROMPT,
    "api": _API_SYSTEM_PROMPT,
    "cache": _CACHE_SYSTEM_PROMPT,
}


def _system_prompt(domain: str) -> str:
    return _DOMAIN_PROMPTS.get(domain, _GENERIC_SYSTEM_PROMPT)


def build_prompt(difference: Difference) -> str:
    """
    Build the full prompt string for this difference.

    The system prompt is domain-specific. The user section carries only
    stable, redacted fields so the same ghost always hashes to the same SHA-256.
    """
    ctx = {k: v for k, v in difference.context.items() if k not in ("safe_fix", "policy")}
    payload = {
        "domain": difference.domain,
        "kind": difference.kind,
        "target": difference.target,
        "context": redact_value(ctx),
    }
    body = json.dumps(payload, sort_keys=True, default=str, indent=2)
    system = _system_prompt(difference.domain)
    return f"{system}\nPROMPT_VERSION: {PROMPT_VERSION}\n\nDIFFERENCE:\n{body}\n"


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def build_api_batch_user_message(
    kind: str,
    fields: list[str],
    path: str,
    file_name: str,
    file_content: str,
    schema_name: str = "",
    operation_id: str = "",
) -> str:
    """
    Build the user-turn message for a batched API patch call.
    """
    target = f"the '{schema_name}' schema" if schema_name else f"the schema for endpoint '{path}'"
    # For main.py patches, use operationId to identify the exact function
    func_name = operation_id if operation_id else path

    if kind in ("FIELD_EXTRA", "FIELD_UNDOCUMENTED"):
        instruction = (
            f"Add these fields to {target} in {file_name}:\n"
            f"  {fields}\n\n"
            f"Add the fields ONLY inside {target}. Do NOT touch any other schema."
        )
    elif kind == "FIELD_MISSING":
        instruction = (
            f"Add these fields to the return dict of the function '{func_name}' in {file_name}:\n"
            f"  {fields}\n\n"
            f"Each field should be added with value None and a '# TODO: implement' comment.\n"
            f"Only modify the return dict of '{func_name}'. Do NOT touch any other function."
        )
    else:
        instruction = (
            f"Fix drift kind={kind} for fields {fields} in {file_name}.\n"
            f"Change only what is directly necessary to fix the drift."
        )

    return (
        f"TASK:\n{instruction}\n\n"
        f"ORIGINAL FILE ({file_name}):\n{file_content}\n\n"
        f"Return the complete patched file as a JSON object:\n"
        f'  {{"action": "patch", "patched_content": "<complete file content>", "rationale": "<what you changed>"}}'
    )
