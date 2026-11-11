"""
Core cache — resolution cache keyed by Difference.signature.

Rules:
  · Cache key = Difference.signature (stable hash, NOT UUID).
  · Each entry carries cache_version, validator_contract, prompt_hash.
  · Lookup skips on any field mismatch (version, contract, prompt_hash).
  · Cache hits are marked cached=True and CACHED badge shown in UI.
  · Demo mode serves pre-recorded resolutions; no live LLM call.
  · The cache is stored as JSON — agents/cache.json by default.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import FilePatch, Resolution, SQLChange

# Bump this when the validator contract or prompt schema changes.
CACHE_VERSION = "1.0.0"

# Increment when the validator's operation matrix or binding rules change.
VALIDATOR_CONTRACT = "v1"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class Cache:
    """
    JSON-backed resolution cache.

    Typical path: agents/cache.json
    """

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        signature: str,
        prompt_hash: str,
    ) -> Resolution | None:
        """
        Return a cached Resolution if the signature matches AND
        cache_version, validator_contract, and prompt_hash all match.
        Returns None on any mismatch (falls through to live call).
        """
        entry = self._data.get(signature)
        if entry is None:
            return None
        if entry.get("cache_version") != CACHE_VERSION:
            return None
        if entry.get("validator_contract") != VALIDATOR_CONTRACT:
            return None
        if entry.get("prompt_hash") != prompt_hash:
            return None
        return self._deserialise(entry)

    def put(self, resolution: Resolution) -> None:
        """Store a Resolution keyed by difference_signature."""
        self._data[resolution.difference_signature] = self._serialise(resolution)
        self._save()

    def has(self, signature: str) -> bool:
        return signature in self._data

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self.cache_path.exists():
            try:
                self._data = json.loads(self.cache_path.read_bytes())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, default=_default_serialiser))
        os.replace(tmp, self.cache_path)

    def _serialise(self, resolution: Resolution) -> dict[str, Any]:
        change = resolution.proposed_change
        serialised_change: dict[str, Any] | None = None
        if isinstance(change, SQLChange):
            serialised_change = {
                "type": "sql",
                "statement": change.statement,
                "operation": change.operation,
                "target": change.target,
            }
        elif isinstance(change, FilePatch):
            serialised_change = {
                "type": "file",
                "file_path": change.file_path,
                "before_sha256": change.before_sha256,
                "after_sha256": change.after_sha256,
                "forward_patch": change.forward_patch.hex(),
                "reverse_patch": change.reverse_patch.hex(),
            }
        return {
            "difference_id": resolution.difference_id,
            "difference_signature": resolution.difference_signature,
            "proposed_change": serialised_change,
            "rationale": resolution.rationale,
            "prompt_sha256": resolution.prompt_sha256,
            "cache_version": CACHE_VERSION,
            "validator_contract": VALIDATOR_CONTRACT,
            "prompt_hash": resolution.prompt_hash,
            "cached": True,
            "resolved_at": resolution.resolved_at.isoformat(),
        }

    def _deserialise(self, entry: dict[str, Any]) -> Resolution:
        change_data = entry.get("proposed_change")
        change: SQLChange | FilePatch | None = None
        if change_data:
            if change_data.get("type") == "sql":
                change = SQLChange(
                    statement=change_data["statement"],
                    operation=change_data["operation"],
                    target=change_data["target"],
                )
            elif change_data.get("type") == "file":
                change = FilePatch(
                    file_path=change_data["file_path"],
                    before_sha256=change_data["before_sha256"],
                    after_sha256=change_data["after_sha256"],
                    forward_patch=bytes.fromhex(change_data["forward_patch"]),
                    reverse_patch=bytes.fromhex(change_data["reverse_patch"]),
                )
        return Resolution(
            difference_id=entry.get("difference_id", ""),
            difference_signature=entry.get("difference_signature", ""),
            proposed_change=change,
            rationale=entry.get("rationale", ""),
            prompt_sha256=entry.get("prompt_sha256", ""),
            cache_version=entry.get("cache_version", ""),
            validator_contract=entry.get("validator_contract", ""),
            prompt_hash=entry.get("prompt_hash", ""),
            cached=True,
            resolved_at=datetime.fromisoformat(
                entry.get("resolved_at", datetime.now(timezone.utc).isoformat())
            ),
        )


def _default_serialiser(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Not serialisable: {type(obj).__name__}")
