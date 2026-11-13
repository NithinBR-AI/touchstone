"""
Core data models — all domain-agnostic.

Rules:
  · GhostState is the single source of truth for state names.
  · ChangeProposal is the generic opaque change carrier — core never inspects payload.
  · Resolution.proposed_change is None for review-only resolutions (no fix available).
  · Every Resolution carries the prompt's SHA-256.
  · VerificationResult carries the exact re-read state, not just a bool.
  · Rollback is an event (RollbackEvent), not a state transition.
  · ValidationResult is the return type of the Validator protocol.
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

    domain: str
    target: str
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
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signature: str = ""  # set by comparator before returning
    domain: str = ""
    kind: str = ""
    target: str = ""
    severity: str = "HIGH"  # "LOW" | "MEDIUM" | "HIGH"
    state: GhostState = GhostState.PENDING
    context: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Generic change proposal — opaque to core
# ---------------------------------------------------------------------------


@dataclass
class ChangeProposal:
    """
    Generic carrier for a proposed change — core never inspects payload contents.

    · operation : domain-owned label (e.g. "CREATE_INDEX", "FILE_PATCH") — treated
                  as an opaque string by core; adapters give it meaning.
    · payload   : opaque dict — domain adapter owns schema and interpretation.
                  Must be JSON-serialisable. Core serialises/deserialises as-is.
    · domain    : informational — core must never use this for routing decisions.
    · target    : informational — mirrors Difference.target for traceability.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    domain: str = ""
    target: str = ""
    operation: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Return type of the Validator protocol."""

    status: str  # "valid" | "blocked" | "review_required" | "invalid"
    reason: str = ""
    ghost_state: GhostState = GhostState.PENDING


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass
class Resolution:
    """
    The resolver's proposal for one Difference.

    · proposed_change is None when no fix is available (REVIEW_REQUIRED / BLOCKED).
      None must never reach execution — the orchestrator enforces this.
    · prompt_sha256 is the SHA-256 of the exact prompt sent to the LLM.
    · cached=True when served from cache (no live LLM call).
    """

    difference_id: str
    difference_signature: str
    proposed_change: ChangeProposal | None  # None = no fix available
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
    Domain-specific details are carried in applied_change.payload.
    """

    difference_id: str
    success: bool
    applied_change: ChangeProposal | None = None
    error_code: str = ""  # never raw exception text
    error_class: str = ""
    error_summary: str = ""  # generated from code lookup table
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
    remaining: Difference | None = None  # populated when resolved=False


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
    Executors that do not implement RollbackSupport always produce success=False.
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
    · requires_refresh: True after any patch succeeds (siblings superseded).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    domain: str = ""
    differences: list[Difference] = field(default_factory=list)
    resolutions: dict[str, Resolution] = field(default_factory=dict)  # keyed by difference_id
    outcomes: dict[str, ApplyOutcome] = field(default_factory=dict)  # keyed by difference_id
    rollback_events: dict[str, list[RollbackEvent]] = field(default_factory=dict)
    requires_refresh: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    audit_log: list[dict[str, Any]] = field(default_factory=list)
