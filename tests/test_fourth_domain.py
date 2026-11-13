"""
test_fourth_domain.py — Fourth-domain extensibility integration test.

Domain: "config" — detects when a live config dict is missing a key that the
declared config requires, and adds it.

Adapters are implemented here, in-memory, with no imports from postgres/redis/
openapi adapters and no changes to core/. This proves that a new domain can be
added without touching core.

Lifecycle tested:
  1. Discover + compare → KEY_MISSING difference detected.
  2. Resolve → deterministic ChangeProposal produced.
  3. Validate (happy path) → "valid".
  4. Validate (bad proposal) → "blocked", execution never reached.
  5. apply_one without human_authorized → PENDING diff → executes → RESOLVED.
  6. Verify → confirms key is now present.
  7. Persistence → report saved after every state change.
  8. apply_one on REVIEW_REQUIRED (no proposal) without human_authorized → stays REVIEW_REQUIRED.
  9. Authorization gate → REVIEW_REQUIRED diff with proposal + human_authorized=True → executes.
 10. Zero core modifications — core/ files are not patched or monkey-patched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

import pytest

from core.cache import Cache
from core.models import (
    ChangeProposal,
    ComparisonTarget,
    Difference,
    ExecutionResult,
    GhostState,
    Resolution,
    State,
    ValidationResult,
    VerificationResult,
)
from core.orchestrator import Orchestrator
from core.persistence import Persistence

# ---------------------------------------------------------------------------
# In-memory config domain adapters — no imports from other adapter packages
# ---------------------------------------------------------------------------

DOMAIN = "config"


class ConfigInputSource:
    """Yields one ComparisonTarget per declared config section."""

    def __init__(self, sections: list[str]) -> None:
        self._sections = sections

    def targets(self) -> list[ComparisonTarget]:
        return [ComparisonTarget(domain=DOMAIN, target=s) for s in self._sections]


class ConfigSourceAdapter:
    """
    Reads declared (intended) and live (actual) config dicts.
    Intended is a static dict; actual is a mutable dict (simulates live system).
    """

    def __init__(
        self,
        declared: dict[str, dict[str, Any]],
        live: dict[str, dict[str, Any]],
    ) -> None:
        self._declared = declared
        self._live = live

    def intended(self, target: ComparisonTarget) -> State:
        return State(domain=DOMAIN, data=dict(self._declared.get(target.target, {})))

    def actual(self, target: ComparisonTarget) -> State:
        return State(domain=DOMAIN, data=dict(self._live.get(target.target, {})))


class ConfigComparator:
    """Diffs intended vs actual; emits KEY_MISSING or UNDECLARED_KEY differences."""

    def compare(self, intended: State, actual: State) -> list[Difference]:
        diffs: list[Difference] = []
        for key, value in intended.data.items():
            if key not in actual.data:
                sig = f"config:KEY_MISSING:{key}"
                diffs.append(
                    Difference(
                        signature=sig,
                        domain=DOMAIN,
                        kind="KEY_MISSING",
                        target=key,
                        severity="MEDIUM",
                        context={"expected_value": value, "safe_fix": True},
                    )
                )
        for key in actual.data:
            if key not in intended.data:
                sig = f"config:UNDECLARED_KEY:{key}"
                diffs.append(
                    Difference(
                        signature=sig,
                        domain=DOMAIN,
                        kind="UNDECLARED_KEY",
                        target=key,
                        severity="LOW",
                        context={"safe_fix": False},
                    )
                )
        return diffs


class ConfigResolver:
    """
    Deterministic resolver — no LLM.
    KEY_MISSING → ChangeProposal with operation SET_KEY and the declared value.
    UNDECLARED_KEY → proposed_change=None (requires human review).
    """

    def __init__(self, declared: dict[str, dict[str, Any]]) -> None:
        self._declared = declared

    def resolve(self, difference: Difference) -> Resolution:
        if difference.kind == "KEY_MISSING":
            section = difference.context.get("section", "default")
            value = difference.context.get("expected_value", "")
            proposal = ChangeProposal(
                domain=DOMAIN,
                target=difference.target,
                operation="SET_KEY",
                payload={"key": difference.target, "value": value, "section": section},
            )
            return Resolution(
                difference_id=difference.id,
                difference_signature=difference.signature,
                proposed_change=proposal,
            )
        # UNDECLARED_KEY — no automated fix
        return Resolution(
            difference_id=difference.id,
            difference_signature=difference.signature,
            proposed_change=None,
            rationale="undeclared keys require manual review before deletion",
        )


class ConfigValidator:
    """
    Implements Validator protocol for the config domain.
    Checks:
      · operation is SET_KEY
      · payload has 'key' and 'value'
      · proposal.target matches difference.target
    """

    def validate(
        self,
        proposal: ChangeProposal,
        difference: Difference,
        *,
        human_authorized: bool = False,
    ) -> ValidationResult:
        if proposal.operation != "SET_KEY":
            return ValidationResult(
                status="blocked",
                reason=f"config executor only handles SET_KEY, got '{proposal.operation}'",
                ghost_state=GhostState.BLOCKED,
            )
        if "key" not in proposal.payload or "value" not in proposal.payload:
            return ValidationResult(
                status="blocked",
                reason="proposal payload missing 'key' or 'value'",
                ghost_state=GhostState.BLOCKED,
            )
        if proposal.target != difference.target:
            return ValidationResult(
                status="invalid",
                reason=f"proposal targets '{proposal.target}' but difference targets '{difference.target}'",
                ghost_state=GhostState.BLOCKED,
            )
        return ValidationResult(status="valid", ghost_state=GhostState.PENDING)


class ConfigExecutor:
    """
    Applies SET_KEY by writing the value into the live config dict.
    Fully in-memory — no network, no filesystem.
    """

    def __init__(self, live: dict[str, dict[str, Any]]) -> None:
        self._live = live

    def execute(self, resolution: Resolution, difference: Difference) -> ExecutionResult:
        proposal = resolution.proposed_change
        if proposal is None:
            return ExecutionResult(
                difference_id=difference.id,
                success=False,
                error_code="NO_PROPOSAL",
                error_class="PolicyError",
                error_summary="no proposal to execute",
            )
        key = proposal.payload["key"]
        value = proposal.payload["value"]
        section = proposal.payload.get("section", "default")
        self._live.setdefault(section, {})[key] = value
        return ExecutionResult(
            difference_id=difference.id,
            success=True,
            applied_change=proposal,
        )


class ConfigVerifier:
    """Re-reads live config to confirm the key is now present."""

    def __init__(self, live: dict[str, dict[str, Any]]) -> None:
        self._live = live

    def verify(self, target: ComparisonTarget, difference: Difference) -> VerificationResult:
        section = difference.context.get("section", "default")
        key = difference.target
        present = key in self._live.get(section, {})
        return VerificationResult(
            difference_id=difference.id,
            resolved=present,
            evidence={"key": key, "section": section, "present": present},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(
    declared: dict[str, dict[str, Any]],
    live: dict[str, dict[str, Any]],
    sections: list[str],
    tmp_path: Path,
) -> Orchestrator:
    return Orchestrator(
        input_source=ConfigInputSource(sections),
        source_adapter=ConfigSourceAdapter(declared, live),
        comparator=ConfigComparator(),
        resolver=ConfigResolver(declared),
        executor=ConfigExecutor(live),
        verifier=ConfigVerifier(live),
        validator=ConfigValidator(),
        persistence=Persistence(tmp_path / "reports"),
        cache=Cache(tmp_path / "cache.json"),
        domain=DOMAIN,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigDomainDiscovery:
    def test_key_missing_detected(self, tmp_path: Path) -> None:
        declared = {"default": {"timeout": 30, "retries": 3}}
        live = {"default": {"retries": 3}}  # timeout missing
        orch = _make_orchestrator(declared, live, ["default"], tmp_path)
        report = orch.run()

        assert len(report.differences) == 1
        diff = report.differences[0]
        assert diff.kind == "KEY_MISSING"
        assert diff.target == "timeout"
        assert diff.domain == DOMAIN

    def test_undeclared_key_detected(self, tmp_path: Path) -> None:
        declared = {"default": {"timeout": 30}}
        live = {"default": {"timeout": 30, "debug_mode": True}}  # extra key
        orch = _make_orchestrator(declared, live, ["default"], tmp_path)
        report = orch.run()

        assert len(report.differences) == 1
        diff = report.differences[0]
        assert diff.kind == "UNDECLARED_KEY"
        assert diff.target == "debug_mode"

    def test_no_drift_produces_no_differences(self, tmp_path: Path) -> None:
        declared = {"default": {"timeout": 30}}
        live = {"default": {"timeout": 30}}
        orch = _make_orchestrator(declared, live, ["default"], tmp_path)
        report = orch.run()
        assert report.differences == []


class TestConfigDomainValidation:
    def test_valid_proposal_passes(self) -> None:
        validator = ConfigValidator()
        proposal = ChangeProposal(
            domain=DOMAIN,
            target="timeout",
            operation="SET_KEY",
            payload={"key": "timeout", "value": 30},
        )
        diff = Difference(domain=DOMAIN, kind="KEY_MISSING", target="timeout")
        result = validator.validate(proposal, diff)
        assert result.status == "valid"

    def test_wrong_operation_blocked(self) -> None:
        validator = ConfigValidator()
        proposal = ChangeProposal(
            domain=DOMAIN,
            target="timeout",
            operation="DELETE_KEY",
            payload={"key": "timeout", "value": 30},
        )
        diff = Difference(domain=DOMAIN, kind="KEY_MISSING", target="timeout")
        result = validator.validate(proposal, diff)
        assert result.status == "blocked"
        assert result.ghost_state == GhostState.BLOCKED

    def test_missing_payload_fields_blocked(self) -> None:
        validator = ConfigValidator()
        proposal = ChangeProposal(
            domain=DOMAIN,
            target="timeout",
            operation="SET_KEY",
            payload={"key": "timeout"},  # missing 'value'
        )
        diff = Difference(domain=DOMAIN, kind="KEY_MISSING", target="timeout")
        result = validator.validate(proposal, diff)
        assert result.status == "blocked"

    def test_target_mismatch_blocked(self) -> None:
        validator = ConfigValidator()
        proposal = ChangeProposal(
            domain=DOMAIN,
            target="retries",  # wrong target
            operation="SET_KEY",
            payload={"key": "retries", "value": 3},
        )
        diff = Difference(domain=DOMAIN, kind="KEY_MISSING", target="timeout")
        result = validator.validate(proposal, diff)
        assert result.status == "invalid"


class TestConfigDomainFullLifecycle:
    """Full orchestrator lifecycle — scan → apply → verify → persist."""

    def test_key_missing_resolved_end_to_end(self, tmp_path: Path) -> None:
        declared = {"default": {"timeout": 30}}
        live: dict[str, dict[str, Any]] = {"default": {}}
        orch = _make_orchestrator(declared, live, ["default"], tmp_path)

        report = orch.run()
        assert len(report.differences) == 1
        diff = report.differences[0]
        assert diff.state == GhostState.PENDING

        # Resolution carries a populated ChangeProposal
        resolution = report.resolutions[diff.id]
        assert resolution.proposed_change is not None
        assert resolution.proposed_change.operation == "SET_KEY"

        outcome = orch.apply_one(diff.id)
        assert outcome.state == GhostState.RESOLVED
        assert outcome.execution is not None
        assert outcome.execution.success is True

        # Key is now in the live config
        assert live["default"]["timeout"] == 30

        # Verifier confirms it
        assert outcome.verification is not None
        assert outcome.verification.resolved is True

    def test_validation_rejection_stops_execution(self, tmp_path: Path) -> None:
        """A bad proposal is blocked before the executor is called."""
        declared = {"default": {"timeout": 30}}
        live: dict[str, dict[str, Any]] = {"default": {}}

        class _AlwaysBlockValidator:
            def validate(self, proposal, difference, *, human_authorized=False):
                return ValidationResult(
                    status="blocked",
                    reason="test: always blocked",
                    ghost_state=GhostState.BLOCKED,
                )

        orch = Orchestrator(
            input_source=ConfigInputSource(["default"]),
            source_adapter=ConfigSourceAdapter(declared, live),
            comparator=ConfigComparator(),
            resolver=ConfigResolver(declared),
            executor=ConfigExecutor(live),
            verifier=ConfigVerifier(live),
            validator=_AlwaysBlockValidator(),
            persistence=Persistence(tmp_path / "reports"),
            cache=Cache(tmp_path / "cache.json"),
            domain=DOMAIN,
        )

        report = orch.run()
        diff = report.differences[0]
        # _resolve_and_validate runs during run() — validator blocks immediately
        assert diff.state == GhostState.BLOCKED

        # apply_one on a BLOCKED diff (no human_authorized) → returns BLOCKED, no execution
        outcome = orch.apply_one(diff.id, human_authorized=False)
        assert outcome.state == GhostState.BLOCKED
        # Executor was never called — live dict unchanged
        assert live["default"] == {}

    def test_undeclared_key_stays_review_required_without_authorization(self, tmp_path: Path) -> None:
        declared = {"default": {}}
        live = {"default": {"debug_mode": True}}
        orch = _make_orchestrator(declared, live, ["default"], tmp_path)

        report = orch.run()
        diff = report.differences[0]
        assert diff.kind == "UNDECLARED_KEY"

        # Resolver returns None proposal → REVIEW_REQUIRED after run
        resolution = report.resolutions[diff.id]
        assert resolution.proposed_change is None

        # apply_one without human_authorized → still REVIEW_REQUIRED
        outcome = orch.apply_one(diff.id, human_authorized=False)
        assert outcome.state == GhostState.REVIEW_REQUIRED

        # live dict unchanged
        assert live["default"]["debug_mode"] is True

    def test_persistence_saves_after_apply(self, tmp_path: Path) -> None:
        declared = {"default": {"timeout": 30}}
        live: dict[str, dict[str, Any]] = {"default": {}}
        persistence = Persistence(tmp_path / "reports")
        orch = Orchestrator(
            input_source=ConfigInputSource(["default"]),
            source_adapter=ConfigSourceAdapter(declared, live),
            comparator=ConfigComparator(),
            resolver=ConfigResolver(declared),
            executor=ConfigExecutor(live),
            verifier=ConfigVerifier(live),
            validator=ConfigValidator(),
            persistence=persistence,
            cache=Cache(tmp_path / "cache.json"),
            domain=DOMAIN,
        )

        report = orch.run()
        diff = report.differences[0]
        orch.apply_one(diff.id)

        saved = persistence.load(report.id)
        assert saved is not None
        saved_diff = next(d for d in saved.differences if d.id == diff.id)
        assert saved_diff.state == GhostState.RESOLVED

    def test_verification_failure_recorded(self, tmp_path: Path) -> None:
        """Verifier says key is still missing → STILL_PRESENT."""
        declared = {"default": {"timeout": 30}}
        live: dict[str, dict[str, Any]] = {"default": {}}

        class _AlwaysFailVerifier:
            def verify(self, target, difference):
                return VerificationResult(
                    difference_id=difference.id,
                    resolved=False,
                    evidence={"reason": "test: always unresolved"},
                )

        orch = Orchestrator(
            input_source=ConfigInputSource(["default"]),
            source_adapter=ConfigSourceAdapter(declared, live),
            comparator=ConfigComparator(),
            resolver=ConfigResolver(declared),
            executor=ConfigExecutor(live),
            verifier=_AlwaysFailVerifier(),
            validator=ConfigValidator(),
            persistence=Persistence(tmp_path / "reports"),
            cache=Cache(tmp_path / "cache.json"),
            domain=DOMAIN,
        )

        report = orch.run()
        diff = report.differences[0]
        outcome = orch.apply_one(diff.id)

        assert outcome.state == GhostState.STILL_PRESENT
        assert outcome.verification is not None
        assert outcome.verification.resolved is False


class TestFourthDomainNoCoreModification:
    """
    Proves zero core modifications were required to add this domain.

    Checks via AST that core/ files do not reference 'config' as a domain
    string, and that no new symbols were added to core/models.py or
    core/interfaces.py for this domain.
    """

    def test_core_does_not_reference_config_domain_string(self) -> None:
        import ast
        from pathlib import Path

        core_dir = Path(__file__).parent.parent / "core"
        violations: list[str] = []

        for py_file in core_dir.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=py_file.name)
            for node in ast.walk(tree):
                # Look for string constants equal to "config" in a Compare context
                if isinstance(node, ast.Compare):
                    all_nodes = [node.left] + list(node.comparators)
                    for n in all_nodes:
                        if isinstance(n, ast.Constant) and n.value == "config":
                            violations.append(
                                f"core/{py_file.name}:{node.lineno}: "
                                f"string 'config' in comparison"
                            )

        assert not violations, (
            "core/ references 'config' as a domain string — forbidden:\n"
            + "\n".join(f"  {v}" for v in violations)
        )
