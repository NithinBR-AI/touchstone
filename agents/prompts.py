"""
agents/prompts.py — Req 15

Builds the resolver prompt. Only the Difference's domain, kind, target, and
context are included. The per-run UUID is excluded so the same ghost always
produces the same prompt and the same prompt SHA-256.
"""

from __future__ import annotations

import hashlib
import json

from agents.redaction import redact_value
from core.models import Difference

PROMPT_VERSION = "resolver-v1"

SYSTEM_PROMPT = """\
You are the Touchstone resolver. Propose exactly one fix for the difference below.

Respond with a single JSON object and nothing else:
{
  "action": "sql" | "review",
  "statement": "<one SQL statement>" | null,
  "operation": "CREATE_INDEX" | "ALTER_COLUMN_TYPE" | null,
  "rationale": "<one or two sentences>"
}

Rules:
- If context.safe_fix is false, respond with action "review" and statement null.
- CREATE INDEX: plain B-tree, non-unique, no WHERE, no expressions,
  name exactly equal to the difference target.
- ALTER COLUMN TYPE: no USING clause; only widen to context.intended_type.
- Never emit DROP, DELETE, TRUNCATE, SET, or more than one statement.
"""


def build_prompt(difference: Difference) -> str:
    payload = {
        "domain": difference.domain,
        "kind": difference.kind,
        "target": difference.target,
        "context": redact_value(difference.context),
    }
    body = json.dumps(payload, sort_keys=True, default=str, indent=2)
    return f"{SYSTEM_PROMPT}\nPROMPT_VERSION: {PROMPT_VERSION}\n\nDIFFERENCE:\n{body}\n"


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()
