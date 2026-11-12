"""
adapters/postgres/actual.py — Req 7

Reads the actual (live) database schema from Postgres via information_schema.

Connection timeouts per spec:
  · connect_timeout=10  (connection-level)
  · statement_timeout=30000ms via connection options (not SET statement)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg

from adapters.postgres.normalize import UnsupportedTypeError, normalize_pg_type
from core.models import ComparisonTarget, State


def load_actual_schema(dsn: str, schema: str = "public") -> State:
    """
    Query information_schema and pg_indexes to build the actual schema state.

    State.data structure matches intended.py exactly so the comparator
    can diff them directly.
    """
    conn = psycopg.connect(
        dsn,
        connect_timeout=10,
        options="-c statement_timeout=30000",
    )
    try:
        tables = _read_tables(conn, schema)
    finally:
        conn.close()

    return State(domain="database", data={"tables": tables})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_tables(conn: psycopg.Connection, schema: str) -> dict[str, Any]:
    tables: dict[str, Any] = {}

    # --- Columns ---
    col_rows = conn.execute(
        """
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.character_maximum_length,
            c.numeric_precision,
            c.numeric_scale,
            c.is_nullable,
            c.column_default,
            c.udt_name
        FROM information_schema.columns c
        WHERE c.table_schema = %s
          AND c.table_name NOT IN (
              SELECT table_name FROM information_schema.views
              WHERE table_schema = %s
          )
        ORDER BY c.table_name, c.ordinal_position
        """,
        (schema, schema),
    ).fetchall()

    for row in col_rows:
        (
            table_name,
            col_name,
            data_type,
            char_max_len,
            num_precision,
            num_scale,
            is_nullable,
            col_default,
            udt_name,
        ) = row

        if table_name not in tables:
            tables[table_name] = {"columns": {}, "indexes": {}, "primary_key": []}

        pg_type = _resolve_pg_type(
            data_type, char_max_len, num_precision, num_scale, udt_name
        )
        try:
            canonical = normalize_pg_type(pg_type)
        except UnsupportedTypeError:
            canonical = f"UNSUPPORTED:{pg_type}"

        tables[table_name]["columns"][col_name] = {
            "type": canonical,
            "nullable": is_nullable == "YES",
            "primary_key": False,  # filled in below
            "default": col_default,
        }

    # --- Primary keys ---
    pk_rows = conn.execute(
        """
        SELECT
            tc.table_name,
            kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %s
        ORDER BY tc.table_name, kcu.ordinal_position
        """,
        (schema,),
    ).fetchall()

    for table_name, col_name in pk_rows:
        if table_name in tables:
            tables[table_name]["primary_key"].append(col_name)
            if col_name in tables[table_name]["columns"]:
                tables[table_name]["columns"][col_name]["primary_key"] = True

    # --- Indexes (from pg_indexes) ---
    idx_rows = conn.execute(
        """
        SELECT
            i.tablename,
            i.indexname,
            i.indexdef
        FROM pg_indexes i
        WHERE i.schemaname = %s
        ORDER BY i.tablename, i.indexname
        """,
        (schema,),
    ).fetchall()

    for table_name, index_name, index_def in idx_rows:
        if table_name not in tables:
            continue
        is_unique = "UNIQUE" in index_def.upper()
        # Extract column names from indexdef: "... ON table USING btree (col1, col2)"
        cols = _parse_index_columns(index_def)
        tables[table_name]["indexes"][index_name] = {
            "columns": cols,
            "unique": is_unique,
        }

    return tables


def _resolve_pg_type(
    data_type: str,
    char_max_len: int | None,
    num_precision: int | None,
    num_scale: int | None,
    udt_name: str,
) -> str:
    """Build the canonical type string from information_schema fields."""
    dt = data_type.lower()

    if dt == "character varying":
        return f"varchar({char_max_len})" if char_max_len else "text"
    if dt == "character":
        return f"varchar({char_max_len})" if char_max_len else "text"
    if dt == "numeric":
        if num_precision is not None:
            scale = num_scale if num_scale is not None else 0
            return f"numeric({num_precision},{scale})"
        return "numeric"
    if dt in ("timestamp without time zone", "timestamp"):
        return "timestamp"
    if dt == "timestamp with time zone":
        return "timestamptz"
    if dt == "time without time zone":
        return "time"
    if dt == "time with time zone":
        return "time"
    if dt == "double precision":
        return "double precision"
    if dt == "USER-DEFINED".lower():
        # e.g. enums, custom types — use udt_name
        return udt_name.lower()
    if dt == "array":
        return f"{udt_name.lstrip('_')}[]"

    return dt


def _parse_index_columns(indexdef: str) -> list[str]:
    """
    Extract column names from a Postgres indexdef string.
    e.g. "CREATE INDEX ix_users_email ON users USING btree (email)"
    → ["email"]
    """
    try:
        # Find the part after the last '('
        inner = indexdef[indexdef.rfind("(") + 1 : indexdef.rfind(")")]
        cols = [c.strip().strip('"').split(" ")[0] for c in inner.split(",")]
        return [c for c in cols if c]
    except Exception:
        return []


class ActualSchemaAdapter:
    """
    SourceAdapter protocol implementation for the actual (live) side.
    """

    def __init__(self, dsn: str, schema: str = "public") -> None:
        self.dsn = dsn
        self.schema = schema

    def intended(self, target: ComparisonTarget) -> State:
        raise NotImplementedError("ActualSchemaAdapter only reads actual state")

    def actual(self, target: ComparisonTarget) -> State:
        return load_actual_schema(self.dsn, self.schema)


def get_dsn_from_env() -> str:
    """Read DATABASE_URL from environment, raise clearly if missing."""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Copy .env_sample to .env and fill in your Neon connection string."
        )
    return dsn
