"""
adapters/postgres/intended.py — Req 5

Reads the declared (intended) database schema from SQLAlchemy models.

Uses importlib.util.spec_from_file_location to load models.py dynamically,
reads Base.metadata.tables, and normalises every column type to canonical form.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from adapters.postgres.normalize import UnsupportedTypeError, normalize_sqlalchemy_type
from core.models import ComparisonTarget, State


def load_intended_schema(models_path: Path) -> State:
    """
    Load SQLAlchemy models from models_path and return a State containing
    the normalised intended schema.

    State.data structure:
    {
        "tables": {
            "<table_name>": {
                "columns": {
                    "<col_name>": {
                        "type": "<canonical_type>",
                        "nullable": bool,
                        "primary_key": bool,
                        "default": str | None,
                    }
                },
                "indexes": {
                    "<index_name>": {
                        "columns": ["col1", "col2"],
                        "unique": bool,
                    }
                },
                "primary_key": ["col1", ...],
            }
        }
    }
    """
    metadata = _load_metadata(models_path)
    tables: dict[str, Any] = {}

    for table_name, table in metadata.tables.items():
        # Strip schema prefix if public
        name = table_name.split(".")[-1] if "." in table_name else table_name

        columns: dict[str, Any] = {}
        for col in table.columns:
            try:
                canonical = normalize_sqlalchemy_type(col.type)
            except UnsupportedTypeError:
                canonical = f"UNSUPPORTED:{type(col.type).__name__}"

            server_default = None
            if col.server_default is not None:
                server_default = str(col.server_default.arg) if hasattr(col.server_default, "arg") else str(col.server_default)

            columns[col.name] = {
                "type": canonical,
                "nullable": col.nullable if col.nullable is not None else True,
                "primary_key": col.primary_key,
                "default": server_default,
            }

        indexes: dict[str, Any] = {}
        for idx in table.indexes:
            idx_cols = [c.name for c in idx.columns]
            indexes[idx.name] = {
                "columns": idx_cols,
                "unique": idx.unique,
            }

        pk_cols = [c.name for c in table.primary_key.columns]

        tables[name] = {
            "columns": columns,
            "indexes": indexes,
            "primary_key": pk_cols,
        }

    return State(domain="database", data={"tables": tables})


def _load_metadata(models_path: Path) -> Any:
    """
    Dynamically import models.py and return the SQLAlchemy MetaData object.
    Looks for Base.metadata or metadata at module level.
    """
    module_name = f"_touchstone_models_{models_path.stem}"

    # Remove cached module if already loaded (allow reload)
    if module_name in sys.modules:
        del sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, models_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load models from {models_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise ImportError(f"Error loading {models_path}: {exc}") from exc

    # Look for Base.metadata
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if hasattr(attr, "metadata") and hasattr(attr.metadata, "tables"):
            return attr.metadata
        if hasattr(attr, "tables") and hasattr(attr, "create_all"):
            return attr

    raise ImportError(
        f"No SQLAlchemy Base or MetaData found in {models_path}. "
        "Make sure the file defines a Base = declarative_base() and model classes."
    )


class IntendedSchemaAdapter:
    """
    SourceAdapter protocol implementation for the intended (declared) side.
    Wraps load_intended_schema for use by the orchestrator.
    """

    def __init__(self, models_path: Path) -> None:
        self.models_path = models_path

    def intended(self, target: ComparisonTarget) -> State:
        return load_intended_schema(self.models_path)

    def actual(self, target: ComparisonTarget) -> State:
        raise NotImplementedError("IntendedSchemaAdapter only reads intended state")
