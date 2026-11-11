"""
adapters/remote_source.py — Fetch a remote file (GitHub raw URL etc.) to a
local temp path so adapters that expect a file path can use it unchanged.

Cached per process: the first fetch for a given URL writes to a temp file;
subsequent calls return the same path until invalidate() is called (e.g. on
/demo/reset so the next compare always re-fetches fresh content).
"""

from __future__ import annotations

import tempfile
import urllib.request
from pathlib import Path

_cache: dict[str, Path] = {}


def fetch(url: str) -> Path:
    """Return a local Path containing the content at url.

    Fetches once per process lifetime unless invalidate() is called.
    Raises urllib.error.URLError on network failure.
    """
    if url in _cache:
        return _cache[url]

    suffix = Path(url.split("?")[0]).suffix or ".tmp"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            tmp.write(resp.read())
    finally:
        tmp.close()

    path = Path(tmp.name)
    _cache[url] = path
    return path


def invalidate(url: str | None = None) -> None:
    """Clear the cache for one URL, or all URLs if url is None."""
    if url is None:
        _cache.clear()
    else:
        _cache.pop(url, None)
