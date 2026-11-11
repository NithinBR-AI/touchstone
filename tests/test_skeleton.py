"""
test_skeleton.py — Req 1, 38

Asserts:
  1. core/ contains exactly 7 files (by name, not count of any file)
  2. No module outside core/ is imported by any module inside core/
  3. All 7 expected filenames are present
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
CORE_DIR = REPO_ROOT / "core"

EXPECTED_FILES = {
    "__init__.py",
    "interfaces.py",
    "models.py",
    "orchestrator.py",
    "validator.py",
    "persistence.py",
    "cache.py",
}

# Stdlib modules that core/ is allowed to import (not exhaustive — we check
# that no *project* package outside core/ is imported).
ALLOWED_STDLIB_PREFIXES = {
    "__future__",
    "abc", "ast", "collections", "contextlib", "dataclasses", "datetime",
    "enum", "functools", "hashlib", "importlib", "inspect", "io", "itertools",
    "json", "logging", "math", "operator", "os", "pathlib", "re", "shutil",
    "stat", "string", "sys", "textwrap", "time", "tomllib", "traceback",
    "typing", "typing_extensions", "uuid", "warnings",
    # pinned third-party allowed inside core:
    "sqlglot",
    "packaging",
    "yaml",     # pyyaml — used in FilePatchValidator
}

PROJECT_PACKAGES = {"adapters", "agents", "apps", "scripts", "inputs", "demo"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSkeletonFileCount:
    def test_exact_seven_files(self) -> None:
        py_files = {p.name for p in CORE_DIR.glob("*.py")}
        assert py_files == EXPECTED_FILES, (
            f"core/ must contain exactly these files:\n"
            f"  expected: {sorted(EXPECTED_FILES)}\n"
            f"  actual:   {sorted(py_files)}"
        )

    def test_no_subdirectories(self) -> None:
        subdirs = [
            p for p in CORE_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"
        ]
        assert subdirs == [], f"core/ must not contain subdirectories: {subdirs}"


class TestCoreImportRule:
    """No module outside core/ may be imported by any module inside core/."""

    @pytest.mark.parametrize("filename", sorted(EXPECTED_FILES))
    def test_no_external_project_imports(self, filename: str) -> None:
        source = (CORE_DIR / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)

        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = self._extract_roots(node)
                for name in names:
                    if name in PROJECT_PACKAGES:
                        violations.append(
                            f"Line {node.lineno}: '{name}' is a project package "
                            "outside core/ — forbidden"
                        )

        assert not violations, (
            f"core/{filename} contains forbidden imports:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    @staticmethod
    def _extract_roots(node: ast.Import | ast.ImportFrom) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name.split(".")[0] for alias in node.names]
        if node.module:
            return [node.module.split(".")[0]]
        return []


class TestCoreImportable:
    """All 7 core modules must be importable without error."""

    @pytest.mark.parametrize("module", [f"core.{f[:-3]}" for f in sorted(EXPECTED_FILES) if f != "__init__.py"])
    def test_importable(self, module: str) -> None:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            pytest.fail(f"{module} failed to import: {exc}")
