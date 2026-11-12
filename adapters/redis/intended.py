"""
adapters/redis/intended.py

Fetches cache_config.yaml from the target repo (via GitHub raw URL) and
returns a State whose data dict has the shape:

  {
    "keys": {
      "users:list":    {"description": "..."},
      "orders:list":   {"description": "..."},
      "products:list": {"description": "..."},
    }
  }

These are the keys that SHOULD exist in Redis per the declared config.
"""

from __future__ import annotations

import urllib.request

import yaml

from core.models import State


def load_from_url(url: str) -> State:
    """Fetch cache_config.yaml from *url* and return an intended State."""
    req = urllib.request.Request(url, headers={"User-Agent": "touchstone/1.0"})
    with urllib.request.urlopen(
        req, timeout=15
    ) as resp:  # noqa: S310 — public GitHub raw URL
        raw = resp.read().decode()
    return _build_state(raw, source=url)


def _build_state(raw: str, source: str) -> State:
    config = yaml.safe_load(raw)
    keys: dict = {}
    for key, meta in (config.get("keys") or {}).items():
        keys[key] = {"description": (meta or {}).get("description", "")}
    return State(domain="cache", data={"keys": keys, "source": source})
