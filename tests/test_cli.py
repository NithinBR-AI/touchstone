"""
Tests for cli.py (`touchstone compare`).

Regression tests for two bugs found when scanning a new models file:
  - the `touchstone` command could not import `cli` (not packaged)
  - `compare` crashed because the comparator returns a list, not a report
No database: the schema loaders and the comparator are replaced.
"""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path

import pytest

import adapters.postgres.actual as pg_actual
import adapters.postgres.comparator as pg_comparator
import adapters.postgres.intended as pg_intended
import cli
from core.models import Difference, GhostState

EXAMPLE_MODELS = (
    Path(__file__).resolve().parent.parent / "examples" / "scan-your-own" / "models.py"
)


def _diff(kind: str, target: str, state: GhostState) -> Difference:
    return Difference(
        signature=f"sig-{target}",
        domain="database",
        kind=kind,
        target=target,
        severity="MEDIUM",
        state=state,
        context={"table": target.split(".")[0]},
    )


@pytest.fixture
def offline(monkeypatch):
    """No DB: fake loaders and a comparator that returns a plain list."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    monkeypatch.setenv("MODELS_PATH", str(EXAMPLE_MODELS))
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    monkeypatch.setattr(pg_intended, "load_intended_schema", lambda path: object())
    monkeypatch.setattr(pg_actual, "load_actual_schema", lambda url: object())
    diffs = [
        _diff("COLUMN_MISSING", "users.phone", GhostState.REVIEW_REQUIRED),
        _diff("TABLE_MISSING", "reviews", GhostState.BLOCKED),
    ]
    monkeypatch.setattr(
        pg_comparator.SchemaComparator, "compare", lambda self, i, a: diffs
    )


def _args(target: str = "") -> argparse.Namespace:
    return argparse.Namespace(target=target, domain="database")


def test_compare_prints_every_difference(offline, capsys):
    assert cli.cmd_compare(_args()) == 0
    out = capsys.readouterr().out
    assert "Found 2 difference(s)" in out
    assert "target=users.phone" in out and "target=reviews" in out


def test_compare_target_filters_and_counts(offline, capsys):
    assert cli.cmd_compare(_args("users.phone")) == 0
    out = capsys.readouterr().out
    assert "Found 1 difference(s)" in out
    assert "target=reviews" not in out


def test_compare_unknown_target_reports_none(offline, capsys):
    assert cli.cmd_compare(_args("nope.nothing")) == 0
    assert "No differences found." in capsys.readouterr().out


def test_example_models_file_exists():
    assert EXAMPLE_MODELS.is_file()


def test_entry_point_targets_cli_main():
    eps = importlib.metadata.distribution("touchstone").entry_points
    ep = next(e for e in eps if e.name == "touchstone")
    assert ep.value == "cli:main"
    assert ep.load() is cli.main
