"""
test_resolver.py — Task 2.12, Req 15

Mock resolver: deterministic, no LLM, no network.
"""

from __future__ import annotations

import re

from agents.prompts import build_prompt, prompt_sha256
from agents.resolver import MockResolver, canned_responder, validate_payload
from core.models import Difference, GhostState
from core.validator import SQLValidator


def _index_diff(**ctx) -> Difference:
    context = {"table": "users", "columns": ["email"], "unique": False,
               "safe_fix": True, "policy": "auto_fixable"}
    context.update(ctx)
    return Difference(signature="sig-idx", domain="database", kind="INDEX_MISSING",
                      target="ix_users_email", context=context)


def _type_diff(old: str, new: str, safe: bool = True) -> Difference:
    return Difference(
        signature="sig-type", domain="database", kind="TYPE_CHANGED", target="users.email",
        state=GhostState.PENDING if safe else GhostState.REVIEW_REQUIRED,
        context={"table": "users", "column": "email", "original_actual_type": old,
                 "intended_type": new, "safe_fix": safe},
    )


def test_index_missing_proposal_passes_validator() -> None:
    diff = _index_diff()
    res = MockResolver().resolve(diff)
    assert res.proposed_change.statement == "CREATE INDEX ix_users_email ON users (email)"
    assert SQLValidator().validate(res.proposed_change, diff).status == "valid"


def test_widening_type_proposal_passes_validator() -> None:
    diff = _type_diff("varchar(100)", "varchar(255)")
    res = MockResolver().resolve(diff)
    assert res.proposed_change.statement == "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"
    assert SQLValidator().validate(res.proposed_change, diff).status == "valid"


def test_not_safe_fix_is_review_only_and_responder_not_called() -> None:
    calls = []
    res = MockResolver(lambda p, d: calls.append(1)).resolve(_type_diff("text", "integer", safe=False))
    assert res.proposed_change is None
    assert calls == []


def test_prompt_hash_recorded_and_stable_across_runs() -> None:
    a, b = _index_diff(), _index_diff()
    assert a.id != b.id
    ra, rb = MockResolver().resolve(a), MockResolver().resolve(b)
    assert re.fullmatch(r"[0-9a-f]{64}", ra.prompt_sha256)
    assert ra.prompt_sha256 == rb.prompt_sha256 == prompt_sha256(build_prompt(a))


def test_prompt_contains_only_difference_fields() -> None:
    diff = _index_diff()
    prompt = build_prompt(diff)
    assert diff.id not in prompt
    assert diff.signature not in prompt
    assert "ix_users_email" in prompt and "INDEX_MISSING" in prompt


def test_invalid_output_retries_once_then_succeeds() -> None:
    calls = []

    def flaky(prompt, diff):
        calls.append(1)
        return "not json" if len(calls) == 1 else canned_responder(prompt, diff)

    res = MockResolver(flaky).resolve(_index_diff())
    assert len(calls) == 2
    assert res.proposed_change is not None


def test_invalid_output_twice_gives_no_change() -> None:
    calls = []

    def broken(prompt, diff):
        calls.append(1)
        return {"action": "sql"}

    res = MockResolver(broken).resolve(_index_diff())
    assert len(calls) == 2
    assert res.proposed_change is None
    assert "schema validation" in res.rationale


def test_identifiers_needing_quotes_are_not_auto_generated() -> None:
    res = MockResolver().resolve(_index_diff(table="Users"))
    assert res.proposed_change is None


def test_validate_payload_rules() -> None:
    assert validate_payload({"action": "review", "statement": None,
                             "operation": None, "rationale": "x"}) == ""
    assert validate_payload({"action": "sql", "statement": "DROP TABLE x",
                             "operation": "DROP_TABLE", "rationale": "x"}) == "invalid operation"
    assert validate_payload({"action": "review", "statement": "SELECT 1",
                             "operation": None, "rationale": "x"}) != ""
    assert validate_payload([]) != ""
