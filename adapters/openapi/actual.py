"""
adapters/openapi/actual.py — Req 9, 10

Probes a live API and builds a core.models.State whose data dict has the shape:

  {
    "paths": {
      "/users": {
        "get": {
          "responses": {
            "200": {
              "schema": { "type": "object", "properties": { ... } }
            }
          }
        }
      }
    }
  }

Schema is inferred from the actual JSON response body (best-effort).
Only 2xx responses are recorded. Only GET endpoints are probed.

The /health warm-up: before probing any path, GET {base_url}/health is called
with up to MAX_WARMUP_SECS of retries to wake the server (Render free tier).

HTTP client: httpx with follow_redirects=False and trust_env=False (Req 10.3, 10.4).
Redirects are treated as non-2xx and the path is skipped.
SSRF validation runs before every request via ssrf.validate().
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from agents.redaction import redact_value
from core.models import State

from .ssrf import validate as ssrf_validate

MAX_WARMUP_SECS = 60
_WARMUP_INTERVAL = 5

import ssl as _ssl
_SSL_CTX = _ssl.create_default_context()
_SSL_CTX.load_default_certs()

_CLIENT = httpx.Client(
    follow_redirects=False,
    trust_env=False,
    timeout=15.0,
    verify=_SSL_CTX,
    headers={"User-Agent": "touchstone/1.0"},
)


def _infer_schema(value: Any) -> dict[str, Any]:
    """Infer a minimal JSON Schema from a Python value."""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, list):
        if value:
            return {"type": "array", "items": _infer_schema(value[0])}
        return {"type": "array"}
    if isinstance(value, dict):
        props = {k: _infer_schema(v) for k, v in value.items()}
        return {"type": "object", "properties": props}
    return {}


def _fetch_json(url: str) -> tuple[int, Any]:
    """
    Fetch *url* and return (status_code, parsed_body).
    Follows same-host redirects (max 5 hops) with SSRF revalidation on each hop.
    Cross-host redirects are treated as non-2xx and the path is skipped.
    """
    import urllib.parse
    original_host = urllib.parse.urlparse(url).netloc
    current_url = url
    for _ in range(5):
        ssrf_validate(current_url)
        try:
            resp = _CLIENT.get(current_url, headers={"Accept": "application/json"})
        except Exception:
            return 0, None
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if not location:
                return resp.status_code, None
            next_url = urllib.parse.urljoin(current_url, location)
            # only follow same-host redirects
            if urllib.parse.urlparse(next_url).netloc != original_host:
                return resp.status_code, None
            current_url = next_url
            continue
        if not (200 <= resp.status_code < 300):
            return resp.status_code, None
        try:
            body = resp.json() if resp.content else None
        except Exception:
            body = None
        return resp.status_code, body
    return 0, None


def _warmup(base_url: str) -> None:
    """
    Ping GET /health until 200 or MAX_WARMUP_SECS elapsed.
    Render free tier cold-starts take up to ~50s.
    """
    health_url = base_url.rstrip("/") + "/health"
    ssrf_validate(health_url)
    deadline = time.monotonic() + MAX_WARMUP_SECS
    while time.monotonic() < deadline:
        try:
            resp = _CLIENT.get(health_url, timeout=10.0)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(_WARMUP_INTERVAL)


def probe(base_url: str, paths: dict[str, Any]) -> State:
    """
    Probe each GET path in *paths* against *base_url* and return an actual State.

    *paths* is the paths dict from the intended State (we probe only declared paths).
    """
    ssrf_validate(base_url)
    _warmup(base_url)

    base = base_url.rstrip("/")
    result: dict[str, Any] = {}

    for path, path_item in paths.items():
        if "get" not in path_item:
            continue
        # Skip single-item endpoints (paths with {param}) — only probe collections
        # to avoid duplicate drift entries for the same field
        if "{" in path:
            continue
        # Replace path params with simple values for probing
        probe_path = path
        import re
        for param in re.findall(r"\{(\w+)\}", path):
            probe_path = probe_path.replace(f"{{{param}}}", "1")

        url = base + probe_path
        try:
            status, body = _fetch_json(url)
        except Exception:
            continue

        if not (200 <= status < 300):
            continue

        # Redact before schema inference — no sensitive data in stored State
        body = redact_value(body)

        # Unwrap list to infer item schema
        if isinstance(body, list) and body:
            schema = {"type": "array", "items": _infer_schema(body[0])}
        elif body is not None:
            schema = _infer_schema(body)
        else:
            schema = {}

        result[path] = {
            "get": {
                "responses": {
                    str(status): {"schema": schema}
                }
            }
        }

    return State(domain="api", data={"paths": result, "source": base_url})
