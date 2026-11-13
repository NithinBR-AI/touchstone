"""
Core cache — resolution cache keyed by Difference.signature.

Rules:
  · Cache key = Difference.signature (stable hash, NOT UUID).
  · Each entry carries cache_version, validator_contract, prompt_hash.
  · Lookup skips on any field mismatch (version, contract, prompt_hash).
  · Cache hits are marked cached=True and CACHED badge shown in UI.
  · ChangeProposal.payload is serialised as-is — core never inspects contents.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.models import ChangeProposal, Resolution

# Bump this when the validator contract or prompt schema changes.
CACHE_VERSION = "2.0.0"

# Increment when the validator's operation matrix or binding rules change.
VALIDATOR_CONTRACT = "v2"

# Verified fixes expire after this many days — forces a fresh LLM call after expiry.
CACHE_TTL_DAYS = 7


class Cache:
    """
    JSON-backed resolution cache.

    ChangeProposal.payload is treated as opaque — serialised and deserialised
    as a plain dict without inspection.
    """

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self._data: dict[str, Any] = {}
        self._load()

    def get(self, signature: str, prompt_hash: str) -> Resolution | None:
        """
        Return a cached Resolution if signature, cache_version, validator_contract,
        and prompt_hash all match and the entry has not expired.
        Returns None on any mismatch.
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
        expires_at_raw = entry.get("expires_at")
        if expires_at_raw:
            expires_at = datetime.fromisoformat(expires_at_raw)
            if datetime.now(timezone.utc) > expires_at:
                del self._data[signature]
                self._save()
                return None
        return self._deserialise(entry)

    def put(self, resolution: Resolution) -> None:
        """Store a Resolution keyed by difference_signature."""
        self._data[resolution.difference_signature] = self._serialise(resolution)
        self._save()

    def has(self, signature: str) -> bool:
        return signature in self._data

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
        proposal = resolution.proposed_change
        serialised_proposal: dict[str, Any] | None = None
        if proposal is not None:
            serialised_proposal = {
                "id": proposal.id,
                "domain": proposal.domain,
                "target": proposal.target,
                "operation": proposal.operation,
                "payload": proposal.payload,  # opaque — stored as-is
            }
        return {
            "difference_id": resolution.difference_id,
            "difference_signature": resolution.difference_signature,
            "proposed_change": serialised_proposal,
            "rationale": resolution.rationale,
            "prompt_sha256": resolution.prompt_sha256,
            "cache_version": CACHE_VERSION,
            "validator_contract": VALIDATOR_CONTRACT,
            "prompt_hash": resolution.prompt_hash,
            "cached": True,
            "resolved_at": resolution.resolved_at.isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)).isoformat(),
        }

    def _deserialise(self, entry: dict[str, Any]) -> Resolution:
        proposal_data = entry.get("proposed_change")
        proposal: ChangeProposal | None = None
        if proposal_data:
            proposal = ChangeProposal(
                id=proposal_data.get("id", ""),
                domain=proposal_data.get("domain", ""),
                target=proposal_data.get("target", ""),
                operation=proposal_data.get("operation", ""),
                payload=proposal_data.get("payload", {}),
            )
        return Resolution(
            difference_id=entry.get("difference_id", ""),
            difference_signature=entry.get("difference_signature", ""),
            proposed_change=proposal,
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
