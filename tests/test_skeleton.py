"""
test_skeleton.py — Req 1, 38

Asserts:
  1. core/ contains exactly 6 files (by name, not count of any file)
  2. No module outside core/ is imported by any module inside core/
  3. All 6 expected filenames are present
  4. core/ never imports sqlglot (domain-specific library)
  5. core/ never compares .domain to a string literal (no domain branching)
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
    "persistence.py",
    "cache.py",
}

# Stdlib modules that core/ is allowed to import (not exhaustive — we check
# that no *project* package outside core/ is imported).
ALLOWED_STDLIB_PREFIXES = {
    "__future__",
    "abc",
    "ast",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "hashlib",
    "importlib",
    "inspect",
    "io",
    "itertools",
    "json",
    "logging",
    "math",
    "operator",
    "os",
    "pathlib",
    "re",
    "shutil",
    "stat",
    "string",
    "sys",
    "textwrap",
    "time",
    "tomllib",
    "traceback",
    "typing",
    "typing_extensions",
    "uuid",
    "warnings",
    # pinned third-party allowed inside core:
    "packaging",
}

PROJECT_PACKAGES = {"adapters", "agents", "apps", "scripts", "inputs", "demo"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSkeletonFileCount:
    def test_exact_six_files(self) -> None:
        py_files = {p.name for p in CORE_DIR.glob("*.py")}
        assert py_files == EXPECTED_FILES, (
            f"core/ must contain exactly these files:\n"
            f"  expected: {sorted(EXPECTED_FILES)}\n"
            f"  actual:   {sorted(py_files)}"
        )

    def test_no_subdirectories(self) -> None:
        subdirs = [
            p
            for p in CORE_DIR.iterdir()
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

        assert not violations, f"core/{filename} contains forbidden imports:\n" + "\n".join(
            f"  {v}" for v in violations
        )

    @staticmethod
    def _extract_roots(node: ast.Import | ast.ImportFrom) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name.split(".")[0] for alias in node.names]
        if node.module:
            return [node.module.split(".")[0]]
        return []


class TestCoreNoDomainStrings:
    """
    core/ must never branch on domain strings.

    Checks two things via AST (no raw text search):
    1. No import of sqlglot anywhere in core/.
    2. No Compare node where the left side is an attribute chain ending in
       .domain and the right side is a string literal.
    """

    @pytest.mark.parametrize("filename", sorted(EXPECTED_FILES))
    def test_no_sqlglot_import(self, filename: str) -> None:
        source = (CORE_DIR / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "sqlglot":
                        violations.append(f"Line {node.lineno}: import sqlglot")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "sqlglot":
                    violations.append(f"Line {node.lineno}: from sqlglot ...")
        assert not violations, (
            f"core/{filename} imports sqlglot (domain library — forbidden in core):\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    @pytest.mark.parametrize("filename", sorted(EXPECTED_FILES))
    def test_no_domain_string_comparison(self, filename: str) -> None:
        """
        Walks the AST looking for Compare nodes of the form:
            <expr>.domain == "some_string"
            <expr>.domain != "some_string"
            <expr>.domain in {"a", "b"}
        These indicate core is branching on domain identity — forbidden.
        """
        source = (CORE_DIR / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
        violations: list[str] = []

        def _is_domain_attr(node: ast.expr) -> bool:
            return isinstance(node, ast.Attribute) and node.attr == "domain"

        def _is_str_literal(node: ast.expr) -> bool:
            return isinstance(node, ast.Constant) and isinstance(node.value, str)

        def _is_str_set_or_tuple(node: ast.expr) -> bool:
            return isinstance(node, (ast.Set, ast.Tuple)) and all(
                _is_str_literal(elt) for elt in node.elts  # type: ignore[union-attr]
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                left = node.left
                for op, comparator in zip(node.ops, node.comparators):
                    if _is_domain_attr(left) and (
                        _is_str_literal(comparator) or _is_str_set_or_tuple(comparator)
                    ):
                        violations.append(
                            f"Line {node.lineno}: domain string comparison "
                            f"('{ast.unparse(node)}')"
                        )
                    # also catch: "postgres" == diff.domain
                    if _is_domain_attr(comparator) and _is_str_literal(left):
                        violations.append(
                            f"Line {node.lineno}: domain string comparison "
                            f"('{ast.unparse(node)}')"
                        )

        assert not violations, (
            f"core/{filename} branches on domain strings — forbidden:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


class TestCoreImportable:
    """All 6 core modules must be importable without error."""

    @pytest.mark.parametrize(
        "module",
        [f"core.{f[:-3]}" for f in sorted(EXPECTED_FILES) if f != "__init__.py"],
    )
    def test_importable(self, module: str) -> None:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            pytest.fail(f"{module} failed to import: {exc}")
