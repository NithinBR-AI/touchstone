"""
tests/test_redaction.py — Req 11, 43

Token-based redaction boundary coverage per spec:
  · Sensitive: authorization, auth, cookie, token, secret, password, passwd,
    pwd, credential, key, apikey, session, access, refresh, bearer, jwt,
    signature, hmac, clientSecret, privateKey, dsn
  · NOT sensitive: monkey, accessibility, author
"""

import pytest

from agents.redaction import (
    SafeError,
    _is_sensitive,
    _split_tokens,
    redact_headers,
    redact_query,
    redact_url_userinfo,
    redact_value,
)


# ---------------------------------------------------------------------------
# Token splitter
# ---------------------------------------------------------------------------

class TestSplitTokens:
    def test_lowercase(self):
        assert _split_tokens("authorization") == ["authorization"]

    def test_titlecase(self):
        assert _split_tokens("Authorization") == ["authorization"]

    def test_camel_case(self):
        assert _split_tokens("clientSecret") == ["client", "secret"]

    def test_pascal_case(self):
        assert _split_tokens("PrivateKey") == ["private", "key"]

    def test_hyphen_separated(self):
        assert _split_tokens("X-API-Key") == ["x", "api", "key"]

    def test_underscore_separated(self):
        assert _split_tokens("client_secret") == ["client", "secret"]

    def test_allcaps(self):
        assert _split_tokens("CLIENTSECRET") == ["clientsecret"]

    def test_no_split_needed(self):
        assert _split_tokens("monkey") == ["monkey"]


# ---------------------------------------------------------------------------
# _is_sensitive boundary cases (from spec)
# ---------------------------------------------------------------------------

class TestIsSensitive:
    # Must trigger redaction
    def test_authorization(self):
        assert _is_sensitive("authorization")

    def test_Authorization_titlecase(self):
        assert _is_sensitive("Authorization")

    def test_client_secret(self):
        assert _is_sensitive("client_secret")

    def test_clientSecret_camel(self):
        assert _is_sensitive("clientSecret")

    def test_CLIENTSECRET_allcaps(self):
        assert _is_sensitive("CLIENTSECRET")

    def test_private_key(self):
        assert _is_sensitive("private_key")

    def test_privateKey_camel(self):
        assert _is_sensitive("privateKey")

    def test_privatekey_lower(self):
        assert _is_sensitive("privatekey")

    def test_api_key(self):
        assert _is_sensitive("api_key")

    def test_dsn(self):
        assert _is_sensitive("dsn")

    def test_password(self):
        assert _is_sensitive("password")

    def test_token(self):
        assert _is_sensitive("token")

    def test_access_token(self):
        assert _is_sensitive("access_token")

    # Must NOT trigger redaction
    def test_monkey(self):
        assert not _is_sensitive("monkey")

    def test_accessibility(self):
        assert not _is_sensitive("accessibility")

    def test_author(self):
        assert not _is_sensitive("author")

    def test_name(self):
        assert not _is_sensitive("name")

    def test_status(self):
        assert not _is_sensitive("status")


# ---------------------------------------------------------------------------
# redact_value — JSON dict/list/scalar
# ---------------------------------------------------------------------------

class TestRedactValue:
    def test_sensitive_key_redacted(self):
        result = redact_value({"authorization": "Bearer abc123"})
        assert result["authorization"] == "***REDACTED***"

    def test_non_sensitive_key_preserved(self):
        result = redact_value({"name": "Alice", "status": "active"})
        assert result == {"name": "Alice", "status": "active"}

    def test_nested_sensitive(self):
        result = redact_value({"user": {"token": "secret123", "id": 1}})
        assert result["user"]["token"] == "***REDACTED***"
        assert result["user"]["id"] == 1

    def test_list_of_dicts(self):
        result = redact_value([{"password": "abc"}, {"name": "Bob"}])
        assert result[0]["password"] == "***REDACTED***"
        assert result[1]["name"] == "Bob"

    def test_scalar_passthrough(self):
        assert redact_value("hello") == "hello"
        assert redact_value(42) == 42
        assert redact_value(None) is None

    def test_depth_truncation(self):
        # Build a dict nested 7 levels deep
        deep = {"k": "v"}
        for _ in range(7):
            deep = {"nested": deep}
        result = redact_value(deep)
        # At depth > 5 the subtree becomes ***TRUNCATED***
        assert "***TRUNCATED***" in str(result)

    def test_camel_case_key(self):
        result = redact_value({"clientSecret": "my-secret"})
        assert result["clientSecret"] == "***REDACTED***"

    def test_mixed_dict(self):
        result = redact_value({
            "id": 1,
            "api_key": "key-123",
            "email": "user@example.com",
        })
        assert result["id"] == 1
        assert result["api_key"] == "***REDACTED***"
        assert result["email"] == "user@example.com"


# ---------------------------------------------------------------------------
# redact_headers
# ---------------------------------------------------------------------------

class TestRedactHeaders:
    def test_authorization_header(self):
        result = redact_headers({"Authorization": "Bearer token123", "Content-Type": "application/json"})
        assert result["Authorization"] == "***REDACTED***"
        assert result["Content-Type"] == "application/json"

    def test_x_api_key(self):
        result = redact_headers({"X-API-Key": "abc", "Accept": "application/json"})
        assert result["X-API-Key"] == "***REDACTED***"
        assert result["Accept"] == "application/json"

    def test_cookie(self):
        result = redact_headers({"Cookie": "session=abc123"})
        assert result["Cookie"] == "***REDACTED***"

    def test_safe_headers_preserved(self):
        result = redact_headers({"User-Agent": "touchstone/1.0", "Accept": "*/*"})
        assert result == {"User-Agent": "touchstone/1.0", "Accept": "*/*"}


# ---------------------------------------------------------------------------
# redact_query
# ---------------------------------------------------------------------------

class TestRedactQuery:
    def test_token_param(self):
        result = redact_query({"token": "abc123", "page": "1"})
        assert result["token"] == "***REDACTED***"
        assert result["page"] == "1"

    def test_api_key_param(self):
        result = redact_query({"api_key": "secret", "format": "json"})
        assert result["api_key"] == "***REDACTED***"
        assert result["format"] == "json"

    def test_safe_params_preserved(self):
        result = redact_query({"page": "2", "limit": "10"})
        assert result == {"page": "2", "limit": "10"}


# ---------------------------------------------------------------------------
# redact_url_userinfo
# ---------------------------------------------------------------------------

class TestRedactUrlUserinfo:
    def test_basic_auth_in_url(self):
        url = "https://user:pass@example.com/api"
        result = redact_url_userinfo(url)
        assert result == "https://***:***@example.com/api"

    def test_no_userinfo(self):
        url = "https://example.com/api"
        assert redact_url_userinfo(url) == url

    def test_dsn_with_credentials(self):
        url = "postgresql://neondb_owner:secret@ep-xxx.neon.tech/neondb"
        result = redact_url_userinfo(url)
        assert "secret" not in result
        assert "***:***@" in result


# ---------------------------------------------------------------------------
# SafeError
# ---------------------------------------------------------------------------

class TestSafeError:
    def test_from_exception(self):
        exc = ConnectionRefusedError("connect failed")
        err = SafeError.from_exception(exc, code="CONNECTION_REFUSED")
        assert err.code == "CONNECTION_REFUSED"
        assert err.error_class == "ConnectionRefusedError"
        assert "refused" in err.summary.lower()
        assert "connect failed" not in err.summary  # raw message never stored

    def test_from_code(self):
        err = SafeError.from_code("SSRF_BLOCKED", error_class="ValueError")
        assert err.code == "SSRF_BLOCKED"
        assert "blocked" in err.summary.lower()

    def test_unknown_code_fallback(self):
        err = SafeError.from_code("NONEXISTENT_CODE")
        assert "unexpected" in err.summary.lower()

    def test_summary_max_200_chars(self):
        err = SafeError.from_code("UNKNOWN")
        assert len(err.summary) <= 200

    def test_raw_exception_message_not_in_summary(self):
        exc = RuntimeError("DB password is hunter2")
        err = SafeError.from_exception(exc, code="UNKNOWN")
        assert "hunter2" not in err.summary
        assert "hunter2" not in err.error_class
