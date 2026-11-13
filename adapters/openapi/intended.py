"""
adapters/openapi/intended.py — Req 9

Parses an OpenAPI 3.x spec (fetched from a remote URL or a local path) into a
core.models.State whose data dict has the shape:

  {
    "paths": {
      "/users": {
        "get": {
          "operationId": "listUsers",
          "responses": {
            "200": {
              "schema": { ... }   # resolved $ref-free inline schema
            }
          }
        }
      },
      ...
    }
  }

Only GET endpoints are considered (Contract Observer checks response shapes).
$ref resolution is single-level only (components/schemas only).
"""

from __future__ import annotations

import copy
import json
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from core.models import State

from .ssrf import validate as ssrf_validate


def _resolve_ref(ref: str, components: dict[str, Any]) -> dict[str, Any]:
    """Resolve a #/components/schemas/<name> reference."""
    if not ref.startswith("#/components/schemas/"):
        return {}
    name = ref.split("/")[-1]
    return copy.deepcopy(components.get("schemas", {}).get(name, {}))


def _inline_refs(schema: Any, components: dict[str, Any], depth: int = 0) -> Any:
    """Recursively replace $ref with the referenced schema (max 5 levels)."""
    if depth > 5:
        return schema
    if isinstance(schema, dict):
        if "$ref" in schema:
            resolved = _resolve_ref(schema["$ref"], components)
            return _inline_refs(resolved, components, depth + 1)
        return {k: _inline_refs(v, components, depth + 1) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_inline_refs(item, components, depth + 1) for item in schema]
    return schema


def _parse_spec(raw: str) -> dict[str, Any]:
    # Try YAML first (superset of JSON)
    try:
        return yaml.safe_load(raw)
    except Exception:
        return json.loads(raw)


def load_from_url(url: str) -> State:
    """Fetch an OpenAPI spec from *url* and return an intended State."""
    ssrf_validate(url)
    req = urllib.request.Request(url, headers={"User-Agent": "touchstone/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — ssrf validated above
        raw = resp.read().decode()
    return _build_state(raw, source=url)


def load_from_path(path: Path) -> State:
    """Load an OpenAPI spec from a local file and return an intended State."""
    raw = path.read_text(encoding="utf-8")
    return _build_state(raw, source=str(path))


def _build_state(raw: str, source: str) -> State:
    spec = _parse_spec(raw)
    components = spec.get("components", {})
    paths_raw: dict[str, Any] = spec.get("paths", {})

    paths: dict[str, Any] = {}
    for path, path_item in paths_raw.items():
        paths[path] = {}
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            responses: dict[str, Any] = {}
            for status, resp_obj in operation.get("responses", {}).items():
                content = resp_obj.get("content", {})
                json_content = content.get("application/json", {})
                schema = json_content.get("schema", {})
                responses[str(status)] = {
                    "schema": _inline_refs(schema, components),
                }
            # Extract schema name from $ref for each response (before inlining)
            schema_names: dict[str, str] = {}
            for status, resp_obj in operation.get("responses", {}).items():
                content = resp_obj.get("content", {})
                raw_schema = content.get("application/json", {}).get("schema", {})
                # Array wrapping: items may carry the $ref
                ref = raw_schema.get("$ref") or raw_schema.get("items", {}).get("$ref", "")
                if ref.startswith("#/components/schemas/"):
                    schema_names[str(status)] = ref.split("/")[-1]

            paths[path][method.lower()] = {
                "operationId": operation.get("operationId", ""),
                "responses": responses,
                "schema_names": schema_names,
            }

    return State(domain="api", data={"paths": paths, "source": source})
