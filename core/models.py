"""
Core data models — all domain-agnostic.

Rules:
  · GhostState is the single source of truth for state names.
  · TypedChange = SQLChange | FilePatch (union, not base class).
  · Resolution.proposed_change is None for review-only resolutions.
  · Every Resolution carries the prompt's SHA-256.
  · VerificationResult carries the exact re-read state, not just a bool.
  · Rollback is an event (RollbackEvent), not a state transition.
  · Database rollback is not supported — RollbackEvent records the refusal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class GhostState(str, Enum):
    PENDING = "PENDING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    RESOLVED = "RESOLVED"
    STILL_PRESENT = "STILL_PRESENT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    SUPERSEDED = "SUPERSEDED"


# ---------------------------------------------------------------------------
# Comparison target
# ---------------------------------------------------------------------------

@dataclass
class ComparisonTarget:
    """Identifies one unit of comparison (e.g. a table column or endpoint)."""

    domain: str          # "database" | "api" | "deps"
    target: str          # e.g. "users.email", "GET /users/{id}", "sqlglot"
    context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State (opaque carrier — adapters define their own subtypes)
# ---------------------------------------------------------------------------

@dataclass
class State:
    """
    Normalised snapshot of one side of a comparison.
    Adapters subclass or wrap this to carry domain-specific data.
    """

    domain: str
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Difference
# ---------------------------------------------------------------------------

@dataclass
class Difference:
    """
    One detected ghost — a gap between intended and actual state.

    · id      : fresh UUID per run (not stable across runs).
    · signature: stable hash of (domain, kind, target, context_key_fields).
                 Used as cache key; must be deterministic for same inputs.
    · context["safe_fix"]: True only for operations in the auto-fixable matrix.
    · context["original_actual_type"]: populated for TYPE_CHANGED differences.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signature: str = ""          # set by comparator before returning
    domain: str = ""             # "database" | "api" | "deps"
    kind: str = ""               # e.g. "COLUMN_MISSING", "INDEX_MISSING"
    target: str = ""             # canonical target string
    severity: str = "HIGH"       # "LOW" | "MEDIUM" | "HIGH"
    state: GhostState = GhostState.PENDING
    context: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Typed changes
# ---------------------------------------------------------------------------

@dataclass
class SQLChange:
    """A single SQL statement to apply to the database."""

    statement: str           # exactly one SQL statement
    operation: str           # e.g. "CREATE_INDEX", "ALTER_COLUMN_TYPE"
    target: str              # must match originating Difference.target exactly


@dataclass
class FilePatch:
    """An atomic patch to a single file."""

    file_path: str
    before_sha256: str       # hex digest of the file before patching
    after_sha256: str        # hex digest of the file after patching
    forward_patch: bytes     # unified diff bytes to apply
    reverse_patch: bytes     # unified diff bytes to roll back


# Union — the only valid proposed_change types
TypedChange = SQLChange | FilePatch


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    """
    The resolver's proposal for one Difference.

    · proposed_change is None for REVIEW_REQUIRED / BLOCKED differences.
    · prompt_sha256 is the SHA-256 of the exact prompt sent to the LLM.
    · cached=True when served from cache (no live LLM call).
    """

    difference_id: str
    difference_signature: str
    proposed_change: TypedChange | None    # None = review-only
    rationale: str = ""
    prompt_sha256: str = ""
    cache_version: str = ""
    validator_contract: str = ""
    prompt_hash: str = ""
    cached: bool = False
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """
    Result of applying a Resolution.

    File domain: rollback_bytes / post_apply_bytes / post_apply_sha256 /
                 file_path_ref are all populated.
    Database domain: rollback_bytes is empty (rollback not supported).
    """

    difference_id: str
    success: bool
    applied_change: TypedChange | None = None
    error_code: str = ""           # never raw exception text
    error_class: str = ""
    error_summary: str = ""        # generated from code lookup table
    # File-domain fields
    rollback_bytes: bytes = b""
    post_apply_bytes: bytes = b""
    post_apply_sha256: str = ""
    file_path_ref: str = ""
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """
    Evidence-backed result of re-reading state after execution.
    resolved=True only when the Difference is no longer present.
    """

    difference_id: str
    resolved: bool
    rechecked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence: dict[str, Any] = field(default_factory=dict)
    remaining: Difference | None = None   # populated when resolved=False


# ---------------------------------------------------------------------------
# Apply outcome
# ---------------------------------------------------------------------------

@dataclass
class ApplyOutcome:
    """Return type of orchestrator.apply_one()."""

    difference_id: str
    state: GhostState
    execution: ExecutionResult | None = None
    verification: VerificationResult | None = None  # None on execution failure


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

@dataclass
class RollbackEvent:
    """
    Rollback is an event, not a state transition.
    Database domain: success=False, reason="rollback not supported for database domain".
    File domain: success reflects whether the atomic write succeeded.
    """

    difference_id: str
    attempted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class Report:
    """
    Full run report — persisted after every state-changing event.

    · rollback_events: keyed by difference_id.
    · requires_refresh: True after any file patch succeeds (siblings superseded).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    domain: str = ""
    differences: list[Difference] = field(default_factory=list)
    resolutions: dict[str, Resolution] = field(default_factory=dict)   # keyed by difference_id
    outcomes: dict[str, ApplyOutcome] = field(default_factory=dict)    # keyed by difference_id
    rollback_events: dict[str, list[RollbackEvent]] = field(default_factory=dict)
    requires_refresh: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    audit_log: list[dict[str, Any]] = field(default_factory=list)
