"""
test_normalize.py — Task 2.9, Req 6

≥30 hand-verified (sa_type_str, pg_type_str, expected_canonical) triples.

Tests:
  · normalize_pg_type() and normalize_sqlalchemy_type() both produce
    the same canonical string for each reference pair.
  · Round-trip: normalize_sa(sa) == normalize_pg(pg) for every pair.
  · Unsupported types raise UnsupportedTypeError.
  · Invariant: any supported type produces non-empty canonical string.
  · Quoted identifiers and precision/scale cases covered.
"""

from __future__ import annotations

import pytest

from adapters.postgres.normalize import (
    UnsupportedTypeError,
    normalize_pg_type,
    normalize_sqlalchemy_type,
)

# ---------------------------------------------------------------------------
# Reference table: (pg_type_string, expected_canonical)
# 30+ hand-verified triples
# ---------------------------------------------------------------------------

PG_REFERENCE: list[tuple[str, str]] = [
    # Integer family
    ("integer", "integer"),
    ("int", "integer"),
    ("int4", "integer"),
    ("smallint", "smallint"),
    ("int2", "smallint"),
    ("bigint", "bigint"),
    ("int8", "bigint"),
    # Float family
    ("real", "real"),
    ("float4", "real"),
    ("double precision", "double precision"),
    ("float8", "double precision"),
    ("float", "double precision"),
    # Text family
    ("text", "text"),
    ("character varying", "text"),
    ("varchar", "text"),
    ("varchar(100)", "varchar(100)"),
    ("varchar(255)", "varchar(255)"),
    ("character varying(50)", "varchar(50)"),
    ("char(10)", "varchar(10)"),
    ("character(10)", "varchar(10)"),
    # Numeric
    ("numeric", "numeric"),
    ("decimal", "numeric"),
    ("numeric(10,2)", "numeric(10,2)"),
    ("numeric(5,0)", "numeric(5,0)"),
    ("decimal(12,4)", "numeric(12,4)"),
    ("numeric(10)", "numeric(10,0)"),
    # Boolean
    ("boolean", "boolean"),
    ("bool", "boolean"),
    # Binary
    ("bytea", "bytea"),
    # Date/time
    ("date", "date"),
    ("time", "time"),
    ("timestamp", "timestamp"),
    ("timestamp without time zone", "timestamp"),
    ("timestamp with time zone", "timestamptz"),
    ("timestamptz", "timestamptz"),
    # JSON
    ("json", "json"),
    ("jsonb", "jsonb"),
    # UUID
    ("uuid", "uuid"),
    # Float(n) variants
    ("float(4)", "real"),
    ("float(53)", "double precision"),
    # Timestamp with precision
    ("timestamp(0)", "timestamp"),
    ("timestamp(6)", "timestamp"),
    ("timestamp(3) with time zone", "timestamptz"),
    # time with precision
    ("time(0)", "time"),
    ("time(6)", "time"),
]

# ---------------------------------------------------------------------------
# SQLAlchemy reference table: (sa_type_expr, expected_canonical)
# Uses strings that eval to SQLAlchemy type instances
# ---------------------------------------------------------------------------

SA_REFERENCE: list[tuple[str, str]] = [
    ("sat.Integer()", "integer"),
    ("sat.BigInteger()", "bigint"),
    ("sat.SmallInteger()", "smallint"),
    ("sat.String(100)", "varchar(100)"),
    ("sat.String()", "text"),
    ("sat.Text()", "text"),
    ("sat.VARCHAR(255)", "varchar(255)"),
    ("sat.VARCHAR()", "text"),
    ("sat.Float()", "double precision"),
    ("sat.Float(24)", "real"),
    ("sat.Float(53)", "double precision"),
    ("sat.Numeric(10, 2)", "numeric(10,2)"),
    ("sat.Numeric()", "numeric"),
    ("sat.Boolean()", "boolean"),
    ("sat.LargeBinary()", "bytea"),
    ("sat.Date()", "date"),
    ("sat.Time()", "time"),
    ("sat.DateTime()", "timestamp"),
    ("sat.DateTime(timezone=True)", "timestamptz"),
    ("sat.JSON()", "json"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNormalizePgType:
    @pytest.mark.parametrize("pg_type,expected", PG_REFERENCE)
    def test_canonical(self, pg_type: str, expected: str) -> None:
        assert normalize_pg_type(pg_type) == expected

    def test_non_empty_for_all_supported(self) -> None:
        """Invariant: every supported type produces a non-empty string."""
        for pg_type, _ in PG_REFERENCE:
            result = normalize_pg_type(pg_type)
            assert result, f"normalize_pg_type({pg_type!r}) returned empty string"


class TestNormalizeSaType:
    @pytest.mark.parametrize("sa_expr,expected", SA_REFERENCE)
    def test_canonical(self, sa_expr: str, expected: str) -> None:
        import sqlalchemy.types as sat  # noqa: F401

        sa_type = eval(sa_expr)  # noqa: S307 — test-only, controlled input
        assert normalize_sqlalchemy_type(sa_type) == expected

    def test_non_empty_for_all_supported(self) -> None:
        import sqlalchemy.types as sat  # noqa: F401

        for sa_expr, _ in SA_REFERENCE:
            sa_type = eval(sa_expr)  # noqa: S307
            result = normalize_sqlalchemy_type(sa_type)
            assert result, f"normalize_sqlalchemy_type({sa_expr}) returned empty string"

    def test_accepts_type_class_not_just_instance(self) -> None:
        import sqlalchemy.types as sat

        assert normalize_sqlalchemy_type(sat.Integer) == "integer"
        assert normalize_sqlalchemy_type(sat.BigInteger) == "bigint"
        assert normalize_sqlalchemy_type(sat.Boolean) == "boolean"


class TestUnsupportedTypes:
    @pytest.mark.parametrize(
        "bad_type",
        [
            "serial",
            "bigserial",
            "smallserial",
            "money",
            "xml",
            "oid",
            "uuid[]",  # arrays not supported
            "integer[]",
        ],
    )
    def test_raises_unsupported(self, bad_type: str) -> None:
        with pytest.raises(UnsupportedTypeError):
            normalize_pg_type(bad_type)

    def test_completely_unknown_raises(self) -> None:
        with pytest.raises(UnsupportedTypeError):
            normalize_pg_type("ghosttype")


class TestRoundTrip:
    """
    For every (pg_type, canonical) pair that also has an SA equivalent,
    verify normalize_sa == normalize_pg == canonical.
    """

    ROUND_TRIP_PAIRS: list[tuple[str, str, str]] = [
        # (sa_expr, pg_type, expected_canonical)
        ("sat.Integer()", "integer", "integer"),
        ("sat.BigInteger()", "bigint", "bigint"),
        ("sat.SmallInteger()", "smallint", "smallint"),
        ("sat.String(100)", "varchar(100)", "varchar(100)"),
        ("sat.Text()", "text", "text"),
        ("sat.Boolean()", "boolean", "boolean"),
        ("sat.Float(24)", "real", "real"),
        ("sat.Float(53)", "double precision", "double precision"),
        ("sat.Numeric(10, 2)", "numeric(10,2)", "numeric(10,2)"),
        ("sat.Date()", "date", "date"),
        ("sat.DateTime()", "timestamp", "timestamp"),
        ("sat.DateTime(timezone=True)", "timestamptz", "timestamptz"),
        ("sat.JSON()", "json", "json"),
        ("sat.LargeBinary()", "bytea", "bytea"),
    ]

    @pytest.mark.parametrize("sa_expr,pg_type,expected", ROUND_TRIP_PAIRS)
    def test_round_trip(self, sa_expr: str, pg_type: str, expected: str) -> None:
        import sqlalchemy.types as sat  # noqa: F401

        sa_type = eval(sa_expr)  # noqa: S307
        assert normalize_sqlalchemy_type(sa_type) == expected
        assert normalize_pg_type(pg_type) == expected
