"""
agents/context7.py — Req 16 (MCP grounding, runtime)

Fetches a short PostgreSQL documentation excerpt from the Context7 REST API
(the same service behind the Context7 MCP server Kiro uses at dev time) so the
database resolver can ground its SQL proposal in real docs.

Rules:
  · Database domain only. One query per difference kind.
  · Docs improve proposal quality only. The validator still decides safety.
  · Outbound HTTP follows tech.md: SSRF check, trust_env=False,
    follow_redirects=False, short timeout.
  · Any failure (network, non-200, SSRF, timeout) returns None. The caller
    proceeds without docs and marks confidence LOW. Nothing here raises.
  · Results are cached per kind in-process: successes for 6h, failures for 60s
    (so a Context7 outage costs one timeout per minute, not one per ghost).
  · CONTEXT7_API_KEY is optional. Without it Context7 applies low rate limits.
    The key is never logged.
"""

from __future__ import annotations

import logging
import os
import ssl
import time
import urllib.parse

import httpx

from adapters.ssrf import SSRFError
from adapters.ssrf import validate as ssrf_validate

logger = logging.getLogger(__name__)

CONTEXT_URL = "https://context7.com/api/v2/context"
PG_LIBRARY_ID = "/websites/postgresql"
TIMEOUT_S = 5.0
MAX_CHARS = 2000
SUCCESS_TTL_S = 6 * 3600
FAILURE_TTL_S = 60

# One documentation query per database difference kind.
KIND_QUERIES: dict[str, str] = {
    "INDEX_MISSING": "CREATE INDEX syntax btree index on table columns",
    "INDEX_EXTRA": "DROP INDEX syntax",
    "TYPE_CHANGED": "ALTER TABLE ALTER COLUMN TYPE change column data type",
    "COLUMN_MISSING": "ALTER TABLE ADD COLUMN syntax nullable default",
    "COLUMN_EXTRA": "ALTER TABLE DROP COLUMN syntax",
    "TABLE_MISSING": "CREATE TABLE syntax",
    "TABLE_EXTRA": "DROP TABLE syntax",
}

_cache: dict[str, tuple[float, str | None]] = {}


def fetch_pg_docs(kind: str) -> str | None:
    """Return a PostgreSQL docs excerpt (≤ MAX_CHARS) for *kind*, or None."""
    query = KIND_QUERIES.get(kind)
    if query is None:
        return None

    cached = _cache.get(kind)
    if cached is not None:
        stored_at, text = cached
        ttl = SUCCESS_TTL_S if text is not None else FAILURE_TTL_S
        if time.monotonic() - stored_at < ttl:
            return text

    text = _fetch(query)
    _cache[kind] = (time.monotonic(), text)
    return text


def clear_cache() -> None:
    _cache.clear()


def _fetch(query: str) -> str | None:
    params = {"libraryId": PG_LIBRARY_ID, "query": query, "type": "txt"}
    url = f"{CONTEXT_URL}?{urllib.parse.urlencode(params)}"
    try:
        ssrf_validate(url)
        status, body = _http_get(url, _headers())
    except SSRFError:
        logger.warning("Context7 URL failed SSRF validation; proceeding without docs")
        return None
    except Exception as exc:  # network errors, timeouts
        logger.warning(
            "Context7 unreachable (%s); proceeding without docs", type(exc).__name__
        )
        return None

    if status != 200 or not body.strip():
        logger.warning("Context7 returned status %s; proceeding without docs", status)
        return None
    return body.strip()[:MAX_CHARS]


def _headers() -> dict[str, str]:
    key = os.environ.get("CONTEXT7_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _make_client() -> httpx.Client:
    # tech.md: no proxy env vars, no automatic redirects. trust_env=False also
    # stops httpx reading SSL_CERT_FILE, so load the OS trust store explicitly
    # (same pattern as adapters/openapi/actual.py and adapters/redis/actual.py).
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.load_default_certs()
    return httpx.Client(
        trust_env=False, follow_redirects=False, timeout=TIMEOUT_S, verify=ssl_ctx
    )


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    with _make_client() as client:
        resp = client.get(url, headers=headers)
        return resp.status_code, resp.text
