"""
test_context7.py — Task 8.2, Req 16
Context7 docs grounding for the database resolver (agents/context7.py).
All HTTP calls are monkey-patched — no network, no CONTEXT7_API_KEY required.
"""

from __future__ import annotations

import json
import ssl

import pytest

from adapters.openapi.ssrf import SSRFError
from agents import context7
from agents.prompts import prompt_sha256
from agents.resolver import LOW_CONFIDENCE_NOTE, MantleResolver
from core.models import Difference


@pytest.fixture(autouse=True)
def _offline_context7(monkeypatch):
    """No real Context7 calls: each test patches _http_get with its own fake."""

    def fake_get(url, headers):
        raise ConnectionError("network disabled in tests")

    context7.clear_cache()
    monkeypatch.setattr("agents.context7._http_get", fake_get)
    monkeypatch.setattr("agents.context7.ssrf_validate", lambda url: url)
    yield
    context7.clear_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOCS = (
    "### Change a column data type with ALTER COLUMN TYPE\n"
    "ALTER TABLE products ALTER COLUMN price TYPE numeric(10,2);"
)


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


# ---------------------------------------------------------------------------
# Resolver grounding — database prompt carries the docs excerpt
# ---------------------------------------------------------------------------


def test_docs_excerpt_is_in_db_prompt(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    called = {}

    def fake_get(url, headers):
        return 200, DOCS

    def fake_ultra(system, user, **kw):
        called["system"] = system
        return _ok_sql_payload()

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert DOCS in called["system"]
    assert "Use it for syntax only" in called["system"]
    assert res.proposed_change is not None
    assert LOW_CONFIDENCE_NOTE not in res.rationale


def test_prompt_sha256_covers_docs(monkeypatch):
    """prompt_sha256 hashes the exact prompt sent, including the docs block."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    called = {}

    def fake_ultra(system, user, **kw):
        called["system"] = system
        return _ok_sql_payload()

    monkeypatch.setattr("agents.context7._http_get", lambda url, headers: (200, DOCS))
    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert res.prompt_sha256 == prompt_sha256(called["system"])


def test_context7_down_resolver_proceeds_with_low_confidence(monkeypatch):
    """Context7 unreachable → no docs, proposal still made, rationale marked LOW."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    called = {}

    def fake_get(url, headers):
        raise ConnectionError("context7 down")

    def fake_ultra(system, user, **kw):
        called["system"] = system
        return _ok_sql_payload()

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert res.proposed_change is not None
    assert LOW_CONFIDENCE_NOTE in res.rationale
    assert "<docs>" not in called["system"]


def test_blocked_rationale_also_marked_low(monkeypatch):
    """The LOW note is carried on every database exit path, including Blocked."""
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")

    def fake_ultra(system, user, **kw):
        return json.dumps({"action": "sql"})  # missing required keys

    monkeypatch.setattr("agents.mantle_client.ultra", fake_ultra)
    monkeypatch.setattr("agents.mantle_client.strip_llm_wrapper", lambda x: x)
    res = MantleResolver().resolve(_db_diff())
    assert res.proposed_change is None
    assert "Blocked" in res.rationale
    assert LOW_CONFIDENCE_NOTE in res.rationale


def test_api_and_cache_domains_do_not_call_context7(monkeypatch):
    monkeypatch.setenv("MANTLE_API_KEY", "test-key")
    calls = []

    def fake_get(url, headers):
        calls.append(1)
        return 200, DOCS

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    MantleResolver().resolve(_api_diff())
    MantleResolver().resolve(_cache_diff())
    assert calls == []


def test_no_api_key_does_not_call_context7(monkeypatch):
    """MockResolver fallback path makes no Context7 request."""
    monkeypatch.delenv("MANTLE_API_KEY", raising=False)
    calls = []

    def fake_get(url, headers):
        calls.append(1)
        return 200, DOCS

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    MantleResolver().resolve(_db_diff())
    assert calls == []


# ---------------------------------------------------------------------------
# fetch_pg_docs — request, truncation, caching, failures
# ---------------------------------------------------------------------------


def test_request_targets_postgres_library(monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["url"] = url
        return 200, DOCS

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    context7.fetch_pg_docs("TYPE_CHANGED")
    assert seen["url"].startswith("https://context7.com/api/v2/context?")
    assert "libraryId=%2Fwebsites%2Fpostgresql" in seen["url"]


def test_excerpt_truncated_to_max_chars(monkeypatch):
    monkeypatch.setattr("agents.context7._http_get", lambda url, headers: (200, "x" * 10_000))
    assert len(context7.fetch_pg_docs("TYPE_CHANGED")) == context7.MAX_CHARS


def test_docs_cached_per_kind(monkeypatch):
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return 200, DOCS

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    context7.fetch_pg_docs("TYPE_CHANGED")
    context7.fetch_pg_docs("TYPE_CHANGED")
    context7.fetch_pg_docs("INDEX_MISSING")
    assert len(calls) == 2  # one request per kind


def test_failure_cached_briefly(monkeypatch):
    """An outage costs one request per minute, not one per ghost."""
    calls = []

    def fake_get(url, headers):
        calls.append(1)
        return 503, ""

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    assert context7.fetch_pg_docs("TYPE_CHANGED") is None
    assert context7.fetch_pg_docs("TYPE_CHANGED") is None
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
def test_non_200_returns_none(monkeypatch, status):
    monkeypatch.setattr("agents.context7._http_get", lambda url, headers: (status, "error"))
    assert context7.fetch_pg_docs("TYPE_CHANGED") is None


def test_empty_body_returns_none(monkeypatch):
    monkeypatch.setattr("agents.context7._http_get", lambda url, headers: (200, "   "))
    assert context7.fetch_pg_docs("TYPE_CHANGED") is None


def test_unknown_kind_makes_no_request(monkeypatch):
    calls = []

    def fake_get(url, headers):
        calls.append(1)
        return 200, DOCS

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    assert context7.fetch_pg_docs("NOT_A_KIND") is None
    assert calls == []


def test_ssrf_failure_returns_none_without_request(monkeypatch):
    calls = []

    def fake_validate(url):
        raise SSRFError("blocked")

    def fake_get(url, headers):
        calls.append(1)
        return 200, DOCS

    monkeypatch.setattr("agents.context7.ssrf_validate", fake_validate)
    monkeypatch.setattr("agents.context7._http_get", fake_get)
    assert context7.fetch_pg_docs("TYPE_CHANGED") is None
    assert calls == []


# ---------------------------------------------------------------------------
# HTTP client and API key — tech.md outbound HTTP rules
# ---------------------------------------------------------------------------


def test_api_key_sent_only_when_set(monkeypatch):
    seen = []

    def fake_get(url, headers):
        seen.append(headers)
        return 200, DOCS

    monkeypatch.setattr("agents.context7._http_get", fake_get)
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    context7.fetch_pg_docs("TYPE_CHANGED")
    context7.clear_cache()
    monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7sk-test")
    context7.fetch_pg_docs("TYPE_CHANGED")
    assert seen[0] == {}
    assert seen[1] == {"Authorization": "Bearer ctx7sk-test"}


def test_http_client_disables_proxies_and_redirects(monkeypatch):
    """trust_env=False, follow_redirects=False, short timeout."""
    seen = {}
    monkeypatch.setattr("agents.context7.httpx.Client", lambda **kw: seen.update(kw))
    context7._make_client()
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert seen["timeout"] == context7.TIMEOUT_S
    assert isinstance(seen["verify"], ssl.SSLContext)  # OS trust store, never verify=False
