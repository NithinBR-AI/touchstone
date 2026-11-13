"""
test_mantle_resolver.py — Task 7.1, Req 15

MantleResolver: real LLM path (Mantle/Bedrock) with fallback to MockResolver.
All LLM calls are monkey-patched — no network, no MANTLE_API_KEY required.
"""

from __future__ import annotations

import json
import os

import pytest

from agents.prompts import _API_SYSTEM_PROMPT, _CACHE_SYSTEM_PROMPT, _DB_SYSTEM_PROMPT, build_prompt
from agents.resolver import MantleResolver
from core.models import Difference, GhostState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_diff(**ctx) -> Difference:
    base = {
        "table": "users",
        "columns": ["email"],
        "unique": False,
        "safe_fix": True,
        "policy": "auto_fixable",
    }
    base.update(ctx)
    return Difference(
        signature="sig-db",
        domain="database",
        kind="INDEX_MISSING",
        target="ix_users_email",
        context=base,
    )


def _api_diff(**ctx) -> Difference:
    base = {"safe_fix": False, "path": "/users", "field": "created_at"}
    base.update(ctx)
    return Difference(
        signature="sig-api",
        domain="api",
        kind="FIELD_EXTRA",
        target="/users.created_at",
        context=base,
    )


def _cache_diff(**ctx) -> Difference:
    base = {"safe_fix": True, "declared_ttl": 300}
    base.update(ctx)
    return Difference(
        signature="sig-cache",
        domain="cache",
        kind="KEY_MISSING",
        target="user:session:token",
        context=base,
    )


def _ok_sql_payload() -> str:
    return json.dumps(
        {
            "action": "sql",
            "statement": "CREATE INDEX ix_users_email ON users (email)",
            "operation": "CREATE_INDEX",
            "rationale": "Index was declared but missing.",
        }
    )


def _ok_review_payload(rationale: str = "LLM rationale.") -> str:
    return json.dumps(
        {
            "action": "review",
            "statement": None,
            "operation": None,
            "rationale": rationale,
        }
    )


# ---------------------------------------------------------------------------
# Fallback behaviour (no MANTLE_API_KEY)
# ---------------------------------------------------------------------------


def test_no_api_key_falls_back_to_mock_resolver(monkeypatch):
    monkeypatch.delenv("MANTLE_API_KEY", raising=False)
    res = MantleResolver().resolve(_db_diff())
    # MockResolver produces a valid SQL change for safe INDEX_MISSING
    assert res.proposed_change is not None
    assert "CREATE INDEX" in res.proposed_change.payload["statement"]


def test_no_api_key_does_not_call_llm(monkeypatch):
    monkeypatch.delenv("MANTLE_API_KEY", raising=False)
    calls = []

    def fake_ultra(*a, **kw):
        calls.append(1)
        return _ok_sql_payload()

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    MantleResolver().resolve(_db_diff())
    assert calls == []


# ---------------------------------------------------------------------------
# Domain routing — database uses ultra(), api uses deepseek_pro()
# ---------------------------------------------------------------------------


def test_database_domain_calls_ultra(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    called = {}

    def fake_ultra(system, user, **kw):
        called["model"] = "ultra"
        called["system"] = system
        return _ok_sql_payload()

    def fake_deepseek(system, user, **kw):
        called["model"] = "deepseek"
        return _ok_review_payload()

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.deepseek_pro", fake_deepseek)
    MantleResolver().resolve(_db_diff())
    assert called.get("model") == "ultra"


def test_api_domain_skips_llm_at_resolve_time(monkeypatch):
    """API domain skips LLM at resolve time — patch is generated at execute time."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    called = {}

    def fake_ultra(system, user, **kw):
        called["model"] = "ultra"
        return _ok_sql_payload()

    def fake_deepseek(system, user, **kw):
        called["model"] = "deepseek"
        return _ok_review_payload()

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.deepseek_pro", fake_deepseek)
    res = MantleResolver().resolve(_api_diff())
    assert called == {}  # no LLM call for api domain at resolve time
    assert res.proposed_change is not None  # lightweight metadata proposal
    assert res.proposed_change.operation == "FILE_PATCH"
    assert "execute time" in res.rationale


def test_cache_domain_no_llm(monkeypatch):
    """Cache domain uses deterministic proposal — no LLM call."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    called = {}

    def fake_ultra(system, user, **kw):
        called["model"] = "ultra"
        return _ok_review_payload()

    def fake_deepseek(system, user, **kw):
        called["model"] = "deepseek"
        return _ok_review_payload()

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.deepseek_pro", fake_deepseek)
    res = MantleResolver().resolve(_cache_diff())
    assert called == {}  # cache is deterministic — no LLM
    assert res.proposed_change is not None
    assert res.proposed_change.operation in ("CACHE_WARM", "CACHE_DELETE")


# ---------------------------------------------------------------------------
# Domain-aware prompts
# ---------------------------------------------------------------------------


def test_database_prompt_contains_db_system_prompt():
    diff = _db_diff()
    prompt = build_prompt(diff)
    assert "CREATE_INDEX" in prompt
    assert "ALTER_COLUMN_TYPE" in prompt
    assert "FIELD_EXTRA" not in prompt


def test_api_prompt_contains_api_system_prompt():
    diff = _api_diff()
    prompt = build_prompt(diff)
    assert "FIELD_EXTRA" in prompt
    assert "FIELD_MISSING" in prompt
    assert "CREATE_INDEX" not in prompt


def test_cache_prompt_contains_cache_system_prompt():
    diff = _cache_diff()
    prompt = build_prompt(diff)
    assert "TTL_DRIFT" in prompt
    assert "KEY_MISSING" in prompt
    assert "CREATE_INDEX" not in prompt


def test_prompt_excludes_uuid_and_signature():
    diff = _db_diff()
    prompt = build_prompt(diff)
    assert diff.id not in prompt
    assert diff.signature not in prompt


def test_prompt_sha256_stable_across_runs():
    import re

    from agents.prompts import prompt_sha256

    a, b = _db_diff(), _db_diff()
    assert a.id != b.id
    pa, pb = build_prompt(a), build_prompt(b)
    assert pa == pb
    assert re.fullmatch(r"[0-9a-f]{64}", prompt_sha256(pa))


# ---------------------------------------------------------------------------
# safe_fix gate — database only
# ---------------------------------------------------------------------------


def test_database_not_safe_fix_calls_llm_for_human_review(monkeypatch):
    """Non-safe-fix database diffs now call LLM so human can see the proposal."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    calls = []

    def fake_ultra(system, user, **kw):
        calls.append(1)
        return _ok_sql_payload()

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    diff = _db_diff(safe_fix=False)
    res = MantleResolver().resolve(diff)
    # LLM is called — proposal is returned for human review before execution
    assert len(calls) == 1
    assert res.proposed_change is not None


def test_api_domain_returns_deferred_rationale(monkeypatch):
    """API diffs skip LLM — rationale indicates fix is deferred to execute time."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    calls = []

    def fake_deepseek(system, user, **kw):
        calls.append(1)
        return _ok_review_payload("Field exists in API but missing from spec.")

    monkeypatch.setattr("agents.mantle_client.deepseek_pro", fake_deepseek)
    monkeypatch.setattr("agents.mantle_client.ultra", lambda *a, **kw: _ok_review_payload())
    diff = _api_diff(safe_fix=False)
    res = MantleResolver().resolve(diff)
    assert len(calls) == 0  # no LLM call — deferred to executor
    assert res.proposed_change is not None  # lightweight metadata proposal
    assert res.proposed_change.operation == "FILE_PATCH"


# ---------------------------------------------------------------------------
# JSON parsing and wrapper stripping
# ---------------------------------------------------------------------------


def test_llm_wrapper_stripped_before_parse(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")

    def fake_ultra(system, user, **kw):
        return f"<think>reasoning here</think>\n```json\n{_ok_sql_payload()}\n```"

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr(
        "agents.mantle_client.strip_llm_wrapper",
        __import__("agents.mantle_client", fromlist=["strip_llm_wrapper"]).strip_llm_wrapper,
    )
    res = MantleResolver().resolve(_db_diff())
    assert res.proposed_change is not None
    assert "CREATE INDEX" in res.proposed_change.payload["statement"]


def test_valid_sql_response_produces_sql_change(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    monkeypatch.setattr("agents.mantle_client.ultra", lambda *a, **kw: _ok_sql_payload())
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert res.proposed_change is not None
    assert res.proposed_change.operation == "CREATE_INDEX"
    assert res.proposed_change.target == "ix_users_email"


def test_valid_review_response_produces_no_change(monkeypatch):
    """review action from LLM produces proposed_change=None."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    monkeypatch.setattr(
        "agents.mantle_client.ultra",
        lambda *a, **kw: _ok_review_payload("Manual review needed."),
    )
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    # Use a database diff — api domain skips LLM entirely at resolve time
    res = MantleResolver().resolve(_db_diff())
    assert res.proposed_change is None
    assert "Manual review" in res.rationale


# ---------------------------------------------------------------------------
# Retry and blocking
# ---------------------------------------------------------------------------


def test_retry_on_bad_json_then_succeeds(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    attempts = []

    def fake_ultra(system, user, **kw):
        attempts.append(1)
        return "not valid json" if len(attempts) == 1 else _ok_sql_payload()

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert len(attempts) == 2
    assert res.proposed_change is not None


def test_blocked_after_two_bad_responses(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    attempts = []

    def fake_ultra(system, user, **kw):
        attempts.append(1)
        return json.dumps({"action": "sql"})  # missing required keys

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert len(attempts) == 2
    assert res.proposed_change is None
    assert "Blocked" in res.rationale


def test_blocked_after_llm_exception(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")

    def fake_ultra(system, user, **kw):
        raise RuntimeError("network failure")

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert res.proposed_change is None
    assert "Blocked" in res.rationale


# ---------------------------------------------------------------------------
# Resolution fields
# ---------------------------------------------------------------------------


def test_resolution_carries_prompt_sha256(monkeypatch):
    import re

    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    monkeypatch.setattr("agents.mantle_client.ultra", lambda *a, **kw: _ok_sql_payload())
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert re.fullmatch(r"[0-9a-f]{64}", res.prompt_sha256)


def test_resolution_difference_id_matches(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    monkeypatch.setattr("agents.mantle_client.ultra", lambda *a, **kw: _ok_sql_payload())
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    diff = _db_diff()
    res = MantleResolver().resolve(diff)
    assert res.difference_id == diff.id
    assert res.difference_signature == diff.signature
