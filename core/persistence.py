"""
Core persistence — save/load Report objects as JSON.

Rules:
  · save() is called on every state-changing path.
  · Reports are stored under <storage_dir>/<report_id>.json.
  · Raw exception text is never persisted.
  · Credentials are never persisted (adapters must redact before calling save).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from core.models import (ApplyOutcome, Difference, ExecutionResult, FilePatch,
                         GhostState, Report, Resolution, RollbackEvent,
                         SQLChange, VerificationResult)

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _default_serialiser(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, GhostState):
        return obj.value
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


def _serialise_report(report: Report) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(report), default=_default_serialiser))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class Persistence:
    """
    Simple file-backed report store.

    Each report is a single JSON file: <storage_dir>/<report_id>.json
    Atomic write via temp file + rename to avoid torn reads.
    """

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, report: Report) -> None:
        """Atomically persist report to disk."""
        target = self.storage_dir / f"{report.id}.json"
        tmp = self.storage_dir / f".{report.id}.tmp"
        payload = json.dumps(
            _serialise_report(report),
            indent=2,
            default=_default_serialiser,
        ).encode()
        tmp.write_bytes(payload)
        os.replace(tmp, target)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, report_id: str) -> Report | None:
        """Load a report by ID. Returns None if not found."""
        path = self.storage_dir / f"{report_id}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_bytes())
        return _deserialise_report(raw)

    def list_ids(self) -> list[str]:
        """Return all persisted report IDs, newest first."""
        paths = sorted(
            self.storage_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [p.stem for p in paths]

    def latest(self) -> Report | None:
        """Return the most recently saved report, or None."""
        ids = self.list_ids()
        return self.load(ids[0]) if ids else None


# ---------------------------------------------------------------------------
# Deserialisation helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime:
    if not value:
        from datetime import timezone

        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value)


def _deserialise_change(data: dict[str, Any] | None) -> SQLChange | FilePatch | None:
    if data is None:
        return None
    if "statement" in data:
        return SQLChange(
            **{k: v for k, v in data.items() if k in SQLChange.__dataclass_fields__}
        )
    if "forward_patch" in data:
        fp = dict(data)
        for key in ("forward_patch", "reverse_patch"):
            val = fp.get(key)
            if isinstance(val, str):
                fp[key] = bytes.fromhex(val)
        return FilePatch(
            **{k: v for k, v in fp.items() if k in FilePatch.__dataclass_fields__}
        )
    return None


def _deserialise_difference(data: dict[str, Any]) -> Difference:
    d = dict(data)
    d["state"] = GhostState(d.get("state", "PENDING"))
    d["detected_at"] = _parse_dt(d.get("detected_at"))
    return Difference(
        **{k: v for k, v in d.items() if k in Difference.__dataclass_fields__}
    )


def _deserialise_resolution(data: dict[str, Any]) -> Resolution:
    d = dict(data)
    d["proposed_change"] = _deserialise_change(d.get("proposed_change"))
    d["resolved_at"] = _parse_dt(d.get("resolved_at"))
    return Resolution(
        **{k: v for k, v in d.items() if k in Resolution.__dataclass_fields__}
    )


def _deserialise_execution(data: dict[str, Any] | None) -> ExecutionResult | None:
    if data is None:
        return None
    d = dict(data)
    d["applied_change"] = _deserialise_change(d.get("applied_change"))
    d["executed_at"] = _parse_dt(d.get("executed_at"))
    for key in ("rollback_bytes", "post_apply_bytes"):
        val = d.get(key)
        if isinstance(val, str):
            d[key] = bytes.fromhex(val)
    return ExecutionResult(
        **{k: v for k, v in d.items() if k in ExecutionResult.__dataclass_fields__}
    )


def _deserialise_verification(data: dict[str, Any] | None) -> VerificationResult | None:
    if data is None:
        return None
    d = dict(data)
    d["rechecked_at"] = _parse_dt(d.get("rechecked_at"))
    remaining = d.get("remaining")
    d["remaining"] = _deserialise_difference(remaining) if remaining else None
    return VerificationResult(
        **{k: v for k, v in d.items() if k in VerificationResult.__dataclass_fields__}
    )


def _deserialise_outcome(data: dict[str, Any]) -> ApplyOutcome:
    d = dict(data)
    d["state"] = GhostState(d.get("state", "PENDING"))
    d["execution"] = _deserialise_execution(d.get("execution"))
    d["verification"] = _deserialise_verification(d.get("verification"))
    return ApplyOutcome(
        **{k: v for k, v in d.items() if k in ApplyOutcome.__dataclass_fields__}
    )


def _deserialise_rollback_event(data: dict[str, Any]) -> RollbackEvent:
    d = dict(data)
    d["attempted_at"] = _parse_dt(d.get("attempted_at"))
    return RollbackEvent(
        **{k: v for k, v in d.items() if k in RollbackEvent.__dataclass_fields__}
    )


def _deserialise_report(data: dict[str, Any]) -> Report:
    report = Report(
        id=data.get("id", ""),
        domain=data.get("domain", ""),
        requires_refresh=data.get("requires_refresh", False),
        created_at=_parse_dt(data.get("created_at")),
        audit_log=data.get("audit_log", []),
    )
    report.differences = [
        _deserialise_difference(d) for d in data.get("differences", [])
    ]
    report.resolutions = {
        k: _deserialise_resolution(v) for k, v in data.get("resolutions", {}).items()
    }
    report.outcomes = {
        k: _deserialise_outcome(v) for k, v in data.get("outcomes", {}).items()
    }
    report.rollback_events = {
        k: [_deserialise_rollback_event(e) for e in events]
        for k, events in data.get("rollback_events", {}).items()
    }
    return report
