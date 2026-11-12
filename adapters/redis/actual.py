"""
adapters/redis/actual.py

Fetches the live Redis key set from the target app's /cache/status endpoint
and returns a State whose data dict has the shape:

  {
    "keys": ["debug:session_dump", "users:list", ...]
  }

These are the keys that ACTUALLY exist in Redis right now.
Uses the target app's /cache/status endpoint — no direct Redis connection
needed from Touchstone.

Warmup: before fetching /cache/status, GET /health is retried for up to
MAX_WARMUP_SECS to wake the server (Render free tier cold-start).
"""

from __future__ import annotations

import ssl
import time
import urllib.parse
from typing import Any

import httpx

from core.models import State

MAX_WARMUP_SECS = 60
_WARMUP_INTERVAL = 5

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.load_default_certs()

_CLIENT = httpx.Client(
    follow_redirects=False,
    trust_env=False,
    timeout=15.0,
    verify=_SSL_CTX,
    headers={"User-Agent": "touchstone/1.0"},
)


def _warmup(base_url: str) -> None:
    """Ping GET /health until 200 or MAX_WARMUP_SECS elapsed."""
    health_url = base_url.rstrip("/") + "/health"
    deadline = time.monotonic() + MAX_WARMUP_SECS
    while time.monotonic() < deadline:
        try:
            resp = _CLIENT.get(health_url, timeout=10.0)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(_WARMUP_INTERVAL)


def load_from_url(cache_status_url: str) -> State:
    """
    Fetch /cache/status from the live target app and return an actual State.
    *cache_status_url* = e.g. https://touchstone-demo-target-repo.onrender.com/cache/status
    """
    parsed = urllib.parse.urlparse(cache_status_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    _warmup(base_url)

    resp = _CLIENT.get(cache_status_url, headers={"Accept": "application/json"})
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    keys: list[str] = data.get("keys", [])
    return State(domain="cache", data={"keys": keys, "source": cache_status_url})
