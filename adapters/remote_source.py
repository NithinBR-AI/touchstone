"""
adapters/remote_source.py — Fetch a remote file (GitHub raw URL etc.) to a
local temp path so adapters that expect a file path can use it unchanged.

Cached per process: the first fetch for a given URL writes to a temp file;
subsequent calls return the same path until invalidate() is called (e.g. on
/demo/reset so the next compare always re-fetches fresh content).

Outbound HTTP follows tech.md: SSRF check before the request, no automatic
redirects, no proxy env vars. This matters most here: Schema Drift executes
the models.py fetched through this module.
"""

from __future__ import annotations

import ssl
import tempfile
from pathlib import Path

import httpx

from adapters.ssrf import validate as ssrf_validate

TIMEOUT_S = 10.0

_cache: dict[str, Path] = {}


def _make_client() -> httpx.Client:
    # trust_env=False also stops httpx reading SSL_CERT_FILE, so load the OS
    # trust store explicitly (same pattern as the other adapters).
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.load_default_certs()
    return httpx.Client(
        trust_env=False, follow_redirects=False, timeout=TIMEOUT_S, verify=ssl_ctx
    )


def fetch(url: str) -> Path:
    """Return a local Path containing the content at url.

    Fetches once per process lifetime unless invalidate() is called.
    Raises SSRFError for a blocked destination, httpx.HTTPStatusError for any
    non-2xx response (including redirects), httpx.HTTPError on network failure.
    """
    if url in _cache:
        return _cache[url]

    ssrf_validate(url)
    with _make_client() as client:
        resp = client.get(url)
        resp.raise_for_status()  # any non-2xx, including an unfollowed 3xx redirect
        content = resp.content

    suffix = Path(url.split("?")[0]).suffix or ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)

    path = Path(tmp.name)
    _cache[url] = path
    return path


def invalidate(url: str | None = None) -> None:
    """Clear the cache for one URL, or all URLs if url is None."""
    if url is None:
        _cache.clear()
    else:
        _cache.pop(url, None)
