"""
agents/redaction.py — Req 11, 12

Token-based redaction applied before persistence, before logging, and before
the resolver sees any data. Covers headers, query strings, URL userinfo, form
bodies, JSON (depth ≤ 5), and error evidence.

SafeError replaces raw exception text in stored evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Sensitive token set (exhaustive per spec)
# ---------------------------------------------------------------------------

SENSITIVE_TOKENS: frozenset[str] = frozenset(
    {
        "authorization",
        "auth",
        "cookie",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "credential",
        "credentials",
        "key",
        "apikey",
        "session",
        "sessionid",
        "access",
        "refresh",
        "bearer",
        "jwt",
        "signature",
        "hmac",
        "clientsecret",
        "privatekey",
        "dsn",
    }
)

_REDACTED = "***REDACTED***"
_TRUNCATED = "***TRUNCATED***"
_MAX_DEPTH = 5


# ---------------------------------------------------------------------------
# Token splitter
# ---------------------------------------------------------------------------


def _split_tokens(key: str) -> list[str]:
    """
    Split a key into lowercase tokens on camelCase boundaries and separators.

    Examples:
        "Authorization"  → ["authorization"]
        "clientSecret"   → ["client", "secret"]
        "X-API-Key"      → ["x", "api", "key"]
        "monkey"         → ["monkey"]
        "accessibility"  → ["accessibility"]
    """
    # Insert separator before each uppercase letter that follows a lowercase letter
    # or before an uppercase followed by lowercase (handles ABCDef → ABC + Def)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    # Split on non-alphanumeric separators
    parts = re.split(r"[^a-zA-Z0-9]+", s)
    return [p.lower() for p in parts if p]


def _is_sensitive(key: str) -> bool:
    """Return True if any token in *key* is in SENSITIVE_TOKENS."""
    return bool(SENSITIVE_TOKENS.intersection(_split_tokens(key)))


# ---------------------------------------------------------------------------
# Core redaction
# ---------------------------------------------------------------------------


def redact_value(value: Any, depth: int = 0) -> Any:
    """
    Recursively redact sensitive values in a dict/list/scalar.

    - dict keys matching SENSITIVE_TOKENS → value replaced with _REDACTED
    - depth > _MAX_DEPTH → entire subtree replaced with _TRUNCATED
    - non-dict/list values are returned as-is (they are field values, not keys)
    """
    if depth > _MAX_DEPTH:
        return _TRUNCATED

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive(k):
                result[k] = _REDACTED
            else:
                result[k] = redact_value(v, depth + 1)
        return result

    if isinstance(value, list):
        return [redact_value(item, depth + 1) for item in value]

    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact sensitive HTTP headers."""
    return {k: (_REDACTED if _is_sensitive(k) else v) for k, v in headers.items()}


def redact_query(params: dict[str, str]) -> dict[str, str]:
    """Redact sensitive query string parameters."""
    return {k: (_REDACTED if _is_sensitive(k) else v) for k, v in params.items()}


def redact_url_userinfo(url: str) -> str:
    """Replace user:pass@ in a URL with ***:***@."""
    return re.sub(r"(\w+://)([^@/]+:[^@/]+)@", r"\1***:***@", url)


# ---------------------------------------------------------------------------
# SafeError — replaces raw exception text in stored evidence
# ---------------------------------------------------------------------------

_ERROR_SUMMARIES: dict[str, str] = {
    "SSRF_BLOCKED": "Request blocked: destination address is not permitted.",
    "CONNECTION_REFUSED": "Connection refused by the target host.",
    "CONNECTION_TIMEOUT": "Connection timed out.",
    "DNS_FAILURE": "Hostname could not be resolved.",
    "TLS_ERROR": "TLS/SSL handshake failed.",
    "HTTP_ERROR": "Target returned an HTTP error response.",
    "PARSE_ERROR": "Response body could not be parsed.",
    "GITHUB_API_ERROR": "GitHub API returned an error.",
    "UNKNOWN": "An unexpected error occurred.",
}


@dataclass
class SafeError:
    """
    Safe representation of an error — never stores raw exception text.

    *code* is a key into _ERROR_SUMMARIES. *error_class* is the exception
    class name (safe — no message). *summary* is a human-readable string
    derived from the lookup table, max 200 chars.
    """

    code: str
    error_class: str
    summary: str

    @classmethod
    def from_exception(cls, exc: Exception, code: str = "UNKNOWN") -> "SafeError":
        summary = _ERROR_SUMMARIES.get(code, _ERROR_SUMMARIES["UNKNOWN"])
        return cls(
            code=code,
            error_class=type(exc).__name__,
            summary=summary[:200],
        )

    @classmethod
    def from_code(cls, code: str, error_class: str = "") -> "SafeError":
        summary = _ERROR_SUMMARIES.get(code, _ERROR_SUMMARIES["UNKNOWN"])
        return cls(code=code, error_class=error_class, summary=summary[:200])
