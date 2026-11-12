"""File patch validation tests — Req 18, task 1.0c."""

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from core.models import Difference, FilePatch, GhostState
from core.validator import FilePatchValidator, ValidationResult


def _can_symlink(tmp_path: Path) -> bool:
    """Check if symlink creation is permitted (requires privilege on Windows)."""
    try:
        link = tmp_path / "_probe_link"
        link.symlink_to(tmp_path / "_probe_target")
        link.unlink(missing_ok=True)
        return True
    except (OSError, NotImplementedError):
        return False


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_patch(
    file_path: str,
    original_bytes: bytes,
    new_bytes: bytes,
) -> FilePatch:
    return FilePatch(
        file_path=file_path,
        before_sha256=sha256(original_bytes),
        after_sha256=sha256(new_bytes),
        forward_patch=new_bytes,  # week-1 stub: forward_patch = full new content
        reverse_patch=original_bytes,
    )


class TestFilePatchValidator:
    """FilePatchValidator — symlink, path confinement, SHA256, format keys."""

    @pytest.fixture
    def tmp_root(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def validator(self, tmp_root):
        return FilePatchValidator(tmp_root)

    # ------------------------------------------------------------------
    # Valid patch
    # ------------------------------------------------------------------

    def test_valid_yaml_patch(self, validator, tmp_root):
        """Valid YAML patch on a single target key passes."""
        original = b"host: old\n"
        new = b"host: new\n"
        p = tmp_root / "config.yml"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="host", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "valid"

    # ------------------------------------------------------------------
    # Symlink rejection
    # ------------------------------------------------------------------

    def test_symlink_rejected(self, validator, tmp_root):
        """Symlinks are rejected BEFORE resolve — before_sha256 is never checked."""
        if not _can_symlink(tmp_root):
            pytest.skip("Symlink creation requires privilege on this OS/user (Windows)")

        real = tmp_root / "real.yml"
        real.write_bytes(b"key: value\n")
        link = tmp_root / "link.yml"
        link.symlink_to(real)

        patch = FilePatch(
            file_path=str(link),
            before_sha256="anything",
            after_sha256="anything",
            forward_patch=b"key: new\n",
            reverse_patch=b"key: value\n",
        )
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="key", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"
        assert "symlink" in result.reason

    # ------------------------------------------------------------------
    # Path confinement
    # ------------------------------------------------------------------

    def test_path_confinement_escape(self, validator, tmp_root):
        """Paths that resolve outside project root are rejected."""
        # Use an absolute path outside tmp_root
        outside = Path(tmp_root).parent / "outside.yml"

        patch = FilePatch(
            file_path=str(outside),
            before_sha256="anything",
            after_sha256="anything",
            forward_patch=b"x: 1\n",
            reverse_patch=b"x: 0\n",
        )
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="x", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"
        assert "project root" in result.reason or "not found" in result.reason

    # ------------------------------------------------------------------
    # SHA-256 mismatch
    # ------------------------------------------------------------------

    def test_before_sha256_mismatch(self, validator, tmp_root):
        """before_sha256 mismatch is rejected — file changed since snapshot."""
        p = tmp_root / "config.yml"
        p.write_bytes(b"key: original\n")

        patch = FilePatch(
            file_path=str(p),
            before_sha256="wronghash",
            after_sha256=sha256(b"key: new\n"),
            forward_patch=b"key: new\n",
            reverse_patch=b"key: original\n",
        )
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="key", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"
        assert "sha256" in result.reason.lower() or "mismatch" in result.reason.lower()

    # ------------------------------------------------------------------
    # Format-aware parsing — YAML
    # ------------------------------------------------------------------

    def test_yaml_single_key_change_valid(self, validator, tmp_root):
        """YAML patch changing only the declared target key is valid."""
        original = b"host: old\nport: 5432\n"
        new = b"host: new\nport: 5432\n"
        p = tmp_root / "db.yml"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="host", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "valid"

    def test_yaml_root_as_list_rejected(self, validator, tmp_root):
        """YAML file whose root is a list (not a mapping) is rejected."""
        original = b"- item1\n- item2\n"
        new = b"- item1\n- item3\n"
        p = tmp_root / "list.yml"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="item1", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"

    def test_yaml_wrong_key_rejected(self, validator, tmp_root):
        """YAML patch modifying a key other than the declared target is rejected."""
        original = b"host: old\nport: 5432\n"
        new = b"host: old\nport: 9999\n"  # changed port, not host
        p = tmp_root / "db.yml"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="host", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"
        assert "port" in result.reason or "target" in result.reason

    # ------------------------------------------------------------------
    # Format-aware parsing — TOML
    # ------------------------------------------------------------------

    def test_toml_single_key_change_valid(self, validator, tmp_root):
        """TOML patch changing only the declared target key is valid."""
        original = b'name = "old"\nversion = "1.0"\n'
        new = b'name = "new"\nversion = "1.0"\n'
        p = tmp_root / "pyproject.toml"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="DECLARED_UNPINNED", target="name", domain="deps", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "valid"

    def test_toml_wrong_key_rejected(self, validator, tmp_root):
        """TOML patch modifying a key other than the declared target is rejected."""
        original = b'name = "old"\nversion = "1.0"\n'
        new = b'name = "old"\nversion = "2.0"\n'  # changed version, not name
        p = tmp_root / "pyproject.toml"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="DECLARED_UNPINNED", target="name", domain="deps", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"

    # ------------------------------------------------------------------
    # Format-aware parsing — requirements.txt
    # ------------------------------------------------------------------

    def test_requirements_single_package_change_valid(self, validator, tmp_root):
        """requirements.txt patch pinning only the declared target is valid."""
        original = b"sqlglot\nhttpx==0.28.0\n"
        new = b"sqlglot==30.29.0\nhttpx==0.28.0\n"
        p = tmp_root / "requirements.txt"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="DECLARED_UNPINNED", target="sqlglot", domain="deps", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "valid"

    def test_requirements_unrelated_line_rejected(self, validator, tmp_root):
        """requirements.txt patch that also changes an unrelated package is rejected."""
        original = b"sqlglot\nhttpx==0.28.0\n"
        new = b"sqlglot==30.29.0\nhttpx==0.29.0\n"  # also bumped httpx
        p = tmp_root / "requirements.txt"
        p.write_bytes(original)

        patch = make_patch(str(p), original, new)
        diff = Difference(
            kind="DECLARED_UNPINNED", target="sqlglot", domain="deps", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"

    # ------------------------------------------------------------------
    # after_sha256 mismatch
    # ------------------------------------------------------------------

    def test_after_sha256_mismatch_rejected(self, validator, tmp_root):
        """after_sha256 mismatch (patch doesn't produce expected bytes) is rejected."""
        original = b"key: original\n"
        p = tmp_root / "config.yml"
        p.write_bytes(original)

        patch = FilePatch(
            file_path=str(p),
            before_sha256=sha256(original),
            after_sha256="wrong_expected_hash",
            forward_patch=b"key: new\n",
            reverse_patch=original,
        )
        diff = Difference(
            kind="FIELD_UNDOCUMENTED", target="key", domain="api", context={}
        )

        result = validator.validate(patch, diff)
        assert result.status == "invalid"
        assert "sha256" in result.reason.lower() or "mismatch" in result.reason.lower()
