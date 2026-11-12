"""
adapters/redis/comparator.py

Diffs intended vs actual cache State and emits Difference objects.

Drift kinds:
  KEY_MISSING      declared in cache_config.yaml but absent from Redis -> PENDING
                   safe_fix=True: executor can SET the key as a placeholder
  UNDECLARED_KEY   exists in Redis but not declared in config           -> BLOCKED
                   safe_fix=False: could be leaking sensitive data, needs review
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.models import Difference, GhostState, State

DOMAIN = "cache"

POLICY_AUTO = "auto_fixable"
POLICY_BLOCKED = "blocked"


def _signature(kind: str, target: str, key_ctx: dict[str, Any]) -> str:
    payload = json.dumps(
        {"domain": DOMAIN, "kind": kind, "target": target, "ctx": key_ctx},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _make(
    kind: str,
    target: str,
    severity: str,
    policy: str,
    context: dict[str, Any],
) -> Difference:
    ctx = dict(context)
    ctx["policy"] = policy
    ctx["safe_fix"] = policy == POLICY_AUTO
    state = GhostState.PENDING if policy == POLICY_AUTO else GhostState.BLOCKED
    return Difference(
        signature=_signature(kind, target, {"key": target}),
        domain=DOMAIN,
        kind=kind,
        target=target,
        severity=severity,
        state=state,
        context=ctx,
    )


class CacheComparator:
    """Comparator protocol implementation for the Redis cache domain."""

    def compare(self, intended: State, actual: State) -> list[Difference]:
        declared: dict[str, Any] = intended.data.get("keys", {})
        actual_keys: set[str] = set(actual.data.get("keys", []))
        declared_keys: set[str] = set(declared.keys())
        diffs: list[Difference] = []

        # Declared but missing from Redis
        for key in sorted(declared_keys - actual_keys):
            diffs.append(
                _make(
                    "KEY_MISSING",
                    key,
                    "MEDIUM",
                    POLICY_AUTO,
                    {
                        "key": key,
                        "description": declared[key].get("description", ""),
                        "hint": "Key declared in cache_config.yaml but not found in Redis",
                    },
                )
            )

        # Exists in Redis but not declared
        for key in sorted(actual_keys - declared_keys):
            diffs.append(
                _make(
                    "UNDECLARED_KEY",
                    key,
                    "HIGH",
                    POLICY_BLOCKED,
                    {
                        "key": key,
                        "hint": "Key exists in Redis but is not declared in cache_config.yaml — possible data leak or orphaned cache entry",
                    },
                )
            )

        return diffs
