"""
adapters/postgres/normalize.py — Task 2.9, Req 6

Type normalisation for the Postgres adapter.

Two entry points:
  · normalize_sqlalchemy_type(sa_type)  — canonical string from a SQLAlchemy type object
  · normalize_pg_type(pg_type_str)      — canonical string from information_schema string

Both return the same canonical form so the comparator can do a direct equality check.

Canonical forms (exhaustive — anything else raises UnsupportedTypeError):
  integer, bigint, smallint, text, boolean, real, double precision,
  numeric, numeric(p,s), varchar(n), bytea, date, time, timestamp,
  timestamptz, json, jsonb, uuid

Rules:
  · Precision/scale on numeric are preserved: numeric(10,2)
  · VARCHAR without length → text
  · CHARACTER VARYING(n) → varchar(n)
  · TIMESTAMP WITH TIME ZONE → timestamptz
  · TIMESTAMP WITHOUT TIME ZONE → timestamp
  · FLOAT4 → real, FLOAT8 → double precision
  · INT / INT4 → integer, INT2 → smallint, INT8 → bigint
  · BOOL → boolean
  · SERIAL / BIGSERIAL / SMALLSERIAL are NOT canonical — raise UnsupportedTypeError
    (they are DDL shortcuts, not storage types; actual.py will see integer/bigint)
"""

from __future__ import annotations

import re


class UnsupportedTypeError(ValueError):
    """Raised when a type string cannot be mapped to a canonical form."""


# ---------------------------------------------------------------------------
# Canonical type map (aliases → canonical)
# ---------------------------------------------------------------------------

_SIMPLE_MAP: dict[str, str] = {
    # Integer family
    "int": "integer",
    "int4": "integer",
    "integer": "integer",
    "int2": "smallint",
    "smallint": "smallint",
    "int8": "bigint",
    "bigint": "bigint",
    # Float family
    "float4": "real",
    "real": "real",
    "float8": "double precision",
    "double precision": "double precision",
    "float": "double precision",
    # Text family
    "text": "text",
    "clob": "text",
    # Boolean
    "bool": "boolean",
    "boolean": "boolean",
    # Binary
    "bytea": "bytea",
    # Date/time
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "timestamptz": "timestamptz",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "time with time zone": "time",
    "time without time zone": "time",
    # JSON
    "json": "json",
    "jsonb": "jsonb",
    # UUID
    "uuid": "uuid",
    # Numeric (no precision/scale)
    "numeric": "numeric",
    "decimal": "numeric",
}

# Types we explicitly refuse (serial aliases etc.)
_UNSUPPORTED: frozenset[str] = frozenset(
    {
        "serial",
        "bigserial",
        "smallserial",
        "money",
        "bit",
        "bit varying",
        "xml",
        "oid",
        "point",
        "line",
        "lseg",
        "box",
        "path",
        "polygon",
        "circle",
        "cidr",
        "inet",
        "macaddr",
        "macaddr8",
        "tsvector",
        "tsquery",
        "int4range",
        "int8range",
        "numrange",
        "tsrange",
        "tstzrange",
        "daterange",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_pg_type(pg_type: str) -> str:
    """
    Normalise a Postgres type string (from information_schema or pg_catalog)
    to a canonical form.

    Raises UnsupportedTypeError for unrecognised or unsupported types.
    """
    normalised = _normalise_raw(pg_type)
    return normalised


def normalize_sqlalchemy_type(sa_type: object) -> str:
    """
    Normalise a SQLAlchemy type object to a canonical string.

    Accepts both type instances and type classes (e.g. Integer or Integer()).

    Raises UnsupportedTypeError for unrecognised or unsupported types.
    """
    # Import here to avoid core/ importing SQLAlchemy at module level
    # (this module lives in adapters/, not core/)
    try:
        import sqlalchemy.types as sat  # type: ignore[import]
    except ImportError as exc:
        raise UnsupportedTypeError("sqlalchemy not installed") from exc

    # Resolve class vs instance
    if isinstance(sa_type, type):
        sa_type = sa_type()

    # VARCHAR / String
    if isinstance(sa_type, (sat.VARCHAR, sat.String)):
        length = getattr(sa_type, "length", None)
        if length is None:
            return "text"
        return f"varchar({length})"

    # CHAR
    if isinstance(sa_type, sat.CHAR):
        length = getattr(sa_type, "length", None)
        return f"varchar({length})" if length else "text"

    # Float — must check BEFORE Numeric since Float inherits from Numeric in SA 2.x
    if isinstance(sa_type, sat.Float):
        precision = getattr(sa_type, "precision", None)
        if precision is not None and precision <= 24:
            return "real"
        return "double precision"

    # REAL
    if hasattr(sat, "REAL") and isinstance(sa_type, sat.REAL):
        return "real"

    # DOUBLE_PRECISION
    if hasattr(sat, "DOUBLE_PRECISION") and isinstance(sa_type, sat.DOUBLE_PRECISION):
        return "double precision"

    # Numeric / Decimal
    if isinstance(sa_type, (sat.NUMERIC, sat.Numeric, sat.DECIMAL)):
        prec = getattr(sa_type, "precision", None)
        scale = getattr(sa_type, "scale", None)
        if prec is not None:
            s = scale if scale is not None else 0
            return f"numeric({prec},{s})"
        return "numeric"

    # Integer family
    if isinstance(sa_type, sat.BigInteger):
        return "bigint"
    if isinstance(sa_type, sat.SmallInteger):
        return "smallint"
    if isinstance(sa_type, sat.Integer):
        return "integer"

    # Boolean
    if isinstance(sa_type, sat.Boolean):
        return "boolean"

    # Text
    if isinstance(sa_type, sat.Text):
        return "text"

    # LargeBinary / BYTEA
    if isinstance(sa_type, (sat.LargeBinary, sat.BLOB)):
        return "bytea"

    # Date / Time
    if isinstance(sa_type, sat.DateTime):
        tz = getattr(sa_type, "timezone", False)
        return "timestamptz" if tz else "timestamp"
    if isinstance(sa_type, sat.Date):
        return "date"
    if isinstance(sa_type, sat.Time):
        return "time"

    # JSON
    if isinstance(sa_type, sat.JSON):
        return "json"

    # UUID
    if hasattr(sat, "Uuid") and isinstance(sa_type, sat.Uuid):
        return "uuid"

    # PostgreSQL dialect types (psycopg / pg8000 / asyncpg)
    type_name = type(sa_type).__name__.lower()
    if type_name in ("uuid",):
        return "uuid"
    if type_name in ("jsonb",):
        return "jsonb"
    if type_name in ("bytea",):
        return "bytea"

    # Fall through to string-based normalisation
    try:
        return _normalise_raw(type_name)
    except UnsupportedTypeError:
        pass

    raise UnsupportedTypeError(
        f"Unsupported SQLAlchemy type: {type(sa_type).__name__!r}"
    )


# ---------------------------------------------------------------------------
# Internal normalisation
# ---------------------------------------------------------------------------


def _normalise_raw(raw: str) -> str:
    """Normalise a raw type string to canonical form."""
    s = raw.strip().lower()

    # Strip array suffix for now (arrays not supported)
    if s.endswith("[]"):
        raise UnsupportedTypeError(f"Array types not supported: {raw!r}")

    # Explicitly unsupported
    if s in _UNSUPPORTED:
        raise UnsupportedTypeError(f"Unsupported type: {raw!r}")

    # Simple lookup first
    if s in _SIMPLE_MAP:
        return _SIMPLE_MAP[s]

    # VARCHAR(n) / CHARACTER VARYING(n)
    m = re.fullmatch(r"(?:character varying|varchar)\((\d+)\)", s)
    if m:
        return f"varchar({m.group(1)})"

    # VARCHAR without length
    if s in ("character varying", "varchar"):
        return "text"

    # CHAR(n) / CHARACTER(n)
    m = re.fullmatch(r"(?:char|character)\((\d+)\)", s)
    if m:
        return f"varchar({m.group(1)})"

    # NUMERIC(p) or NUMERIC(p,s)
    m = re.fullmatch(r"(?:numeric|decimal)\((\d+)(?:,\s*(\d+))?\)", s)
    if m:
        p = m.group(1)
        sc = m.group(2) if m.group(2) is not None else "0"
        return f"numeric({p},{sc})"

    # FLOAT(n)
    m = re.fullmatch(r"float\((\d+)\)", s)
    if m:
        n = int(m.group(1))
        return "real" if n <= 24 else "double precision"

    # TIMESTAMP(p) variants
    m = re.fullmatch(r"timestamp\((\d+)\)(?: without time zone)?", s)
    if m:
        return "timestamp"
    m = re.fullmatch(r"timestamp\((\d+)\) with time zone", s)
    if m:
        return "timestamptz"

    # TIME(p) variants
    m = re.fullmatch(r"time\((\d+)\)(?: without time zone)?", s)
    if m:
        return "time"

    raise UnsupportedTypeError(f"Unrecognised type: {raw!r}")
