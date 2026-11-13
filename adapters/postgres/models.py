"""
adapters/postgres/models.py

PostgreSQL-domain change types. These are owned by the postgres adapter —
core knows nothing about SQL statements or file patches.

SQLChange: a single SQL DDL statement to apply to the database.
FilePatch: an atomic patch to a single file (used by openapi adapter too,
           imported from here since file-patch execution lives in openapi adapter).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SQLChange:
    """A single SQL statement to apply to the database."""

    statement: str  # exactly one SQL statement
    operation: str  # e.g. "CREATE_INDEX", "ALTER_COLUMN_TYPE"
    target: str  # must match originating Difference.target exactly


@dataclass
class FilePatch:
    """An atomic patch to a single file."""

    file_path: str
    content: str = ""  # full patched file content
    before_sha256: str = ""
    after_sha256: str = ""
    forward_patch: bytes = field(default_factory=bytes)
    reverse_patch: bytes = field(default_factory=bytes)
