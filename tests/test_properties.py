"""
test_properties.py — Task 9.1, Req 41

Property-based tests (Hypothesis). Each test states a rule that must hold for
every generated input, not just hand-picked examples.

  1. Type normalizer     — reference table, case/whitespace invariance,
                           generated varchar/numeric, SA ↔ PG round-trip,
                           unsupported types raise.
  2. SQL validator       — generated valid SQL for every auto-fixable matrix
                           row is accepted; every mutation is rejected.
                           Generated SQL executes against real Postgres
                           (Neon) on throwaway pbt_* tables only.
  3. State machine       — all 56 cells (7 states × 8 events) match the table
                           in design.md, with and without human_authorized;
                           invariants hold over random event sequences and
                           persisted state matches in-memory state.
  4. Redaction           — required key variants are redacted; substring
                           traps (monkey, accessibility, author) are not;
                           generated camelCase/underscore/hyphen variants;
                           secret values never survive any nesting depth.
  5. SSRF guard          — every address in every blocked range of tech.md
                           is refused, including IPv4-mapped IPv6; public
                           addresses pass.

The real-Postgres tests skip when DATABASE_URL is unset.
"""

from __future__ import annotations

import atexit
import ipaddress
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest
import sqlalchemy.types as sat
from dotenv import load_dotenv
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from adapters.openapi import ssrf
from adapters.postgres.executor import SQLExecutor
from adapters.postgres.normalize import (
    UnsupportedTypeError,
    normalize_pg_type,
    normalize_sqlalchemy_type,
)
from adapters.postgres.validator import SQLValidator
from agents.redaction import redact_headers, redact_value
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
from tests import test_normalize as _norm

PG_REFERENCE = _norm.PG_REFERENCE
SA_REFERENCE = _norm.SA_REFERENCE
ROUND_TRIP_PAIRS = _norm.TestRoundTrip.ROUND_TRIP_PAIRS

load_dotenv()
DB_URL = os.environ.get("DATABASE_URL", "")

# ===========================================================================
# 1. Type normalizer
# ===========================================================================


def _random_case(draw, text: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(c.upper() if f else c for c, f in zip(text, flips))


@st.composite
def _pg_reference_spelling(draw):
    """A reference PG type written with random letter case and outer whitespace."""
    pg_type, expected = draw(st.sampled_from(PG_REFERENCE))
    pad_l = draw(st.sampled_from(["", " ", "  ", "\t"]))
    pad_r = draw(st.sampled_from(["", " ", "\n"]))
    return pad_l + _random_case(draw, pg_type) + pad_r, expected


@given(_pg_reference_spelling())
def test_pg_reference_table_ignores_case_and_outer_whitespace(case):
    spelled, expected = case
    assert normalize_pg_type(spelled) == expected


@given(st.sampled_from(SA_REFERENCE))
def test_sa_reference_table(case):
    sa_expr, expected = case
    assert normalize_sqlalchemy_type(eval(sa_expr)) == expected  # noqa: S307 — controlled input


@given(st.sampled_from(ROUND_TRIP_PAIRS))
def test_round_trip_sa_equals_pg(case):
    sa_expr, pg_type, expected = case
    assert normalize_sqlalchemy_type(eval(sa_expr)) == normalize_pg_type(pg_type) == expected  # noqa: S307


@given(st.integers(min_value=1, max_value=10_485_760), st.sampled_from(["varchar", "character varying"]))
def test_generated_varchar_round_trip(n, spelling):
    canonical = f"varchar({n})"
    assert normalize_pg_type(f"{spelling}({n})") == canonical
    assert normalize_sqlalchemy_type(sat.String(n)) == canonical


@given(
    st.integers(min_value=1, max_value=1000).flatmap(
        lambda p: st.tuples(st.just(p), st.integers(min_value=0, max_value=p))
    ),
    st.sampled_from(["", " "]),
)
def test_generated_numeric_round_trip(ps, space):
    p, s = ps
    canonical = f"numeric({p},{s})"
    assert normalize_pg_type(f"numeric({p},{space}{s})") == canonical
    assert normalize_sqlalchemy_type(sat.Numeric(p, s)) == canonical


@given(st.sampled_from(PG_REFERENCE))
def test_supported_types_always_non_empty(case):
    pg_type, _ = case
    assert normalize_pg_type(pg_type).strip()


@given(st.sampled_from(PG_REFERENCE))
def test_array_of_any_supported_type_raises(case):
    pg_type, _ = case
    with pytest.raises(UnsupportedTypeError):
        normalize_pg_type(pg_type + "[]")


@given(st.from_regex(r"zz[a-z_]{0,12}", fullmatch=True))
def test_unknown_type_names_raise(name):
    with pytest.raises(UnsupportedTypeError):
        normalize_pg_type(name)


# ===========================================================================
# 2. SQL validator — positive + negative, and real Postgres execution
# ===========================================================================

_ident = st.from_regex(r"[a-z]{1,8}", fullmatch=True)
_table = _ident.map(lambda s: f"pbt_{s}")
_column = _ident.map(lambda s: f"c_{s}")

_WIDENING = st.one_of(
    st.just(("smallint", "integer")),
    st.just(("integer", "bigint")),
    st.just(("real", "double precision")),
    st.integers(min_value=1, max_value=1000).flatmap(
        lambda n: st.integers(min_value=n + 1, max_value=10_485_760).map(
            lambda m: (f"varchar({n})", f"varchar({m})")
        )
    ),
    st.integers(min_value=1, max_value=1000).map(lambda n: (f"varchar({n})", "text")),
)

_NARROWING = st.one_of(
    st.just(("integer", "smallint")),
    st.just(("bigint", "integer")),
    st.just(("double precision", "real")),
    st.integers(min_value=2, max_value=1000).flatmap(
        lambda m: st.integers(min_value=1, max_value=m).map(
            lambda n: (f"varchar({m})", f"varchar({n})")  # includes same length
        )
    ),
    st.integers(min_value=1, max_value=1000).map(lambda n: ("text", f"varchar({n})")),
)


@st.composite
def _index_case(draw):
    table = draw(_table)
    cols = draw(st.lists(_column, min_size=1, max_size=3, unique=True))
    name = f"ix_{table}_{'_'.join(cols)}"
    using = draw(st.sampled_from(["", "USING btree "]))  # explicit btree is still plain
    sql = f"CREATE INDEX {name} ON {table} {using}({', '.join(cols)})"
    diff = Difference(
        domain="database",
        kind="INDEX_MISSING",
        target=name,
        context={"table": table, "columns": cols, "unique": False, "safe_fix": True},
    )
    return {"diff": diff, "sql": sql, "table": table, "cols": cols, "name": name}


@st.composite
def _type_case(draw, pairs=_WIDENING):
    table, col = draw(_table), draw(_column)
    old, new = draw(pairs)
    diff = Difference(
        domain="database",
        kind="TYPE_CHANGED",
        target=f"{table}.{col}",
        context={"table": table, "column": col, "original_actual_type": old, "safe_fix": True},
    )
    sql = f"ALTER TABLE {table} ALTER COLUMN {col} TYPE {new}"
    return {"diff": diff, "sql": sql, "table": table, "col": col, "old": old, "new": new}


def _proposal(diff: Difference, sql: str, operation: str) -> ChangeProposal:
    return ChangeProposal(
        domain="database",
        target=diff.target,
        operation=operation,
        payload={"statement": sql, "operation": operation},
    )


def _status(diff: Difference, sql: str, operation: str, *, human_authorized: bool = False) -> str:
    return SQLValidator().validate(
        _proposal(diff, sql, operation), diff, human_authorized=human_authorized
    ).status


@given(_index_case())
def test_valid_create_index_accepted(case):
    assert _status(case["diff"], case["sql"], "CREATE_INDEX") == "valid"


@given(_type_case())
def test_valid_widening_accepted(case):
    assert _status(case["diff"], case["sql"], "ALTER_COLUMN_TYPE") == "valid"


def _first_col_with(cols: list[str], suffix: str) -> str:
    """Column list with *suffix* (e.g. ' DESC') on the first column only."""
    return ", ".join([cols[0] + suffix] + cols[1:])


def _index_mutations(case) -> list[tuple[str, str]]:
    t, cols, name = case["table"], case["cols"], case["name"]
    col_list = ", ".join(cols)
    return [
        ("wrong table", f"CREATE INDEX {name} ON {t}x ({col_list})"),
        ("wrong table, unnamed", f"CREATE INDEX ON {t}x ({col_list})"),
        ("wrong column", f"CREATE INDEX {name} ON {t} ({cols[0]}x)"),
        ("extra column", f"CREATE INDEX {name} ON {t} ({col_list}, c_extra)"),
        ("wrong index name", f"CREATE INDEX {name}x ON {t} ({col_list})"),
        ("extra statement", f"{case['sql']}; DROP TABLE {t}"),
        ("expression index", f"CREATE INDEX {name} ON {t} (lower({cols[0]}))"),
        ("partial index", f"{case['sql']} WHERE {cols[0]} IS NOT NULL"),
        ("unique index", case["sql"].replace("CREATE INDEX", "CREATE UNIQUE INDEX")),
        ("non-public schema", f"CREATE INDEX {name} ON other.{t} ({col_list})"),
        ("delete", f"DELETE FROM {t}"),
        ("truncate", f"TRUNCATE TABLE {t}"),
        ("set", "SET statement_timeout = 0"),
        ("drop table", f"DROP TABLE {t}"),
        ("wrong operation", f"ALTER TABLE {t} ALTER COLUMN {cols[0]} TYPE bigint"),
        # Gaps PBT found in the validator (now fixed):
        ("unnamed", f"CREATE INDEX ON {t} ({col_list})"),
        ("non-btree", f"CREATE INDEX {name} ON {t} USING hash ({col_list})"),
        ("desc", f"CREATE INDEX {name} ON {t} ({_first_col_with(cols, ' DESC')})"),
        ("nulls first", f"CREATE INDEX {name} ON {t} ({_first_col_with(cols, ' NULLS FIRST')})"),
    ] + (
        [("reversed column order", f"CREATE INDEX {name} ON {t} ({', '.join(reversed(cols))})")]
        if len(cols) > 1
        else []
    )


def _type_mutations(case) -> list[tuple[str, str]]:
    t, c, new = case["table"], case["col"], case["new"]
    return [
        ("wrong table", f"ALTER TABLE {t}x ALTER COLUMN {c} TYPE {new}"),
        ("wrong column", f"ALTER TABLE {t} ALTER COLUMN {c}x TYPE {new}"),
        ("extra statement", f"{case['sql']}; DROP TABLE {t}"),
        ("using clause", f"{case['sql']} USING {c}::text"),
        ("non-public schema", f"ALTER TABLE other.{t} ALTER COLUMN {c} TYPE {new}"),
        ("delete", f"DELETE FROM {t}"),
        ("truncate", f"TRUNCATE TABLE {t}"),
        ("set", "SET statement_timeout = 0"),
        ("drop column", f"ALTER TABLE {t} DROP COLUMN {c}"),
        ("wrong operation", f"CREATE INDEX ix_{t}_{c} ON {t} ({c})"),
    ]


@given(_index_case())
def test_mutated_create_index_rejected_even_with_human_authorization(case):
    for label, sql in _index_mutations(case):
        for auth in (False, True):
            status = _status(case["diff"], sql, "CREATE_INDEX", human_authorized=auth)
            assert status != "valid", f"{label} accepted (human_authorized={auth}): {sql}"


@given(_type_case())
def test_mutated_alter_type_rejected_even_with_human_authorization(case):
    for label, sql in _type_mutations(case):
        for auth in (False, True):
            status = _status(case["diff"], sql, "ALTER_COLUMN_TYPE", human_authorized=auth)
            assert status != "valid", f"{label} accepted (human_authorized={auth}): {sql}"


@given(_type_case(pairs=_NARROWING))
def test_non_widening_needs_human_authorization(case):
    """Non-widening pairs are never auto-fixable; only a human can approve them."""
    diff, sql = case["diff"], case["sql"]
    assert _status(diff, sql, "ALTER_COLUMN_TYPE") == "review_required"
    assert _status(diff, sql, "ALTER_COLUMN_TYPE", human_authorized=True) == "valid"


# --- Real Postgres execution (Neon) ---------------------------------------

needs_db = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set — skipping real Postgres")
# Few examples: each costs a fresh Neon connection (~3 s) inside SQLExecutor.
_db_settings = settings(
    max_examples=3, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


_shared_conn: psycopg.Connection | None = None


def _setup_conn() -> psycopg.Connection:
    """One reused connection for setup/cleanup (Neon connects take ~3 s each)."""
    global _shared_conn
    if _shared_conn is None or _shared_conn.closed:
        _shared_conn = psycopg.connect(DB_URL, connect_timeout=10)
    return _shared_conn


def _ddl(conn, sql: str, table: str) -> None:
    """Run setup/cleanup DDL. Refuses anything that isn't a pbt_* table."""
    assert table.startswith("pbt_"), f"refusing DDL on non-test table {table!r}"
    conn.execute(sql)
    conn.commit()


def _run_through_executor(diff: Difference, sql: str, operation: str) -> ExecutionResult:
    resolution = Resolution(
        difference_id=diff.id,
        difference_signature=diff.signature,
        proposed_change=_proposal(diff, sql, operation),
        prompt_sha256="pbt",
    )
    return SQLExecutor(DB_URL).execute(resolution, diff)


@needs_db
@_db_settings
@given(_index_case())
def test_generated_create_index_executes_on_postgres(case):
    table, cols = case["table"], case["cols"]
    assert _status(case["diff"], case["sql"], "CREATE_INDEX") == "valid"
    conn = _setup_conn()
    _ddl(conn, f"DROP TABLE IF EXISTS {table}", table)
    _ddl(conn, f"CREATE TABLE {table} ({', '.join(f'{c} integer' for c in cols)})", table)
    try:
        result = _run_through_executor(case["diff"], case["sql"], "CREATE_INDEX")
        assert result.success, result.error_code
        indexes = [r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename=%s",
            (table,),
        )]
        assert indexes == [case["name"]]
    finally:
        conn.rollback()
        _ddl(conn, f"DROP TABLE IF EXISTS {table}", table)


@needs_db
@_db_settings
@given(_type_case())
def test_generated_widening_executes_on_postgres(case):
    table, col, old, new = case["table"], case["col"], case["old"], case["new"]
    assert _status(case["diff"], case["sql"], "ALTER_COLUMN_TYPE") == "valid"
    sample = "'a'" if old.startswith("varchar") else "1"
    conn = _setup_conn()
    _ddl(conn, f"DROP TABLE IF EXISTS {table}", table)
    _ddl(conn, f"CREATE TABLE {table} ({col} {old})", table)
    _ddl(conn, f"INSERT INTO {table} ({col}) VALUES ({sample})", table)
    try:
        result = _run_through_executor(case["diff"], case["sql"], "ALTER_COLUMN_TYPE")
        assert result.success, result.error_code
        (actual,) = conn.execute(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=%s AND a.attname=%s",
            (table, col),
        ).fetchone()
        assert normalize_pg_type(actual) == normalize_pg_type(new)
    finally:
        conn.rollback()
        _ddl(conn, f"DROP TABLE IF EXISTS {table}", table)


# ===========================================================================
# 3. State machine — 56 cells + invariants over random sequences
# ===========================================================================

VALID = ValidationResult(status="valid", ghost_state=GhostState.PENDING)
REVIEW = ValidationResult(status="review_required", reason="pbt", ghost_state=GhostState.REVIEW_REQUIRED)
BLOCK = ValidationResult(status="blocked", reason="pbt", ghost_state=GhostState.BLOCKED)

TERMINAL = {
    GhostState.RESOLVED,
    GhostState.STILL_PRESENT,
    GhostState.EXECUTION_FAILED,
    GhostState.SUPERSEDED,
}
GATED = {GhostState.REVIEW_REQUIRED, GhostState.BLOCKED}


class _Validator:
    def __init__(self) -> None:
        self.result = VALID

    def validate(self, proposal, difference, *, human_authorized: bool = False):
        return self.result


class _Executor:
    def __init__(self) -> None:
        self.mode = "ok"  # ok | fail | raise

    def execute(self, resolution, difference):
        if self.mode == "raise":
            raise RuntimeError("pbt executor raised")
        return ExecutionResult(
            difference_id=difference.id,
            success=self.mode == "ok",
            applied_change=resolution.proposed_change,
            error_code="" if self.mode == "ok" else "PBT_FAIL",
        )


class _RollbackExecutor(_Executor):
    def rollback(self, execution: ExecutionResult) -> bool:
        return True


class _Verifier:
    def __init__(self) -> None:
        self.resolved = True

    def verify(self, target, difference):
        return VerificationResult(difference_id=difference.id, resolved=self.resolved)


class _Comparator:
    def compare(self, intended, actual):
        return [
            Difference(
                domain="database",
                kind="TYPE_CHANGED",
                target="users.email",
                signature="pbt-sig",
                context={"original_actual_type": "varchar(50)"},
            )
        ]


class _Resolver:
    def resolve(self, difference):
        return Resolution(
            difference_id=difference.id,
            difference_signature=difference.signature,
            proposed_change=ChangeProposal(
                domain="database",
                target=difference.target,
                operation="ALTER_COLUMN_TYPE",
                payload={"statement": "ALTER TABLE users ALTER COLUMN email TYPE varchar(255)"},
            ),
            prompt_sha256="pbt",
        )


# One temp root for every report the state-machine tests persist; removed at exit.
_REPORTS_ROOT = tempfile.mkdtemp(prefix="pbt-reports-")
atexit.register(shutil.rmtree, _REPORTS_ROOT, ignore_errors=True)


def _make(*, rollback_support: bool):
    input_source = MagicMock()
    input_source.targets.return_value = [ComparisonTarget(domain="database", target="public")]
    source = MagicMock()
    source.intended.return_value = State(domain="database")
    source.actual.return_value = State(domain="database")
    cache = MagicMock(spec=Cache)
    cache.get.return_value = None
    parts = {
        "validator": _Validator(),
        "executor": _RollbackExecutor() if rollback_support else _Executor(),
        "verifier": _Verifier(),
        "persistence": Persistence(Path(tempfile.mkdtemp(dir=_REPORTS_ROOT))),
    }
    orch = Orchestrator(
        input_source=input_source,
        source_adapter=source,
        comparator=_Comparator(),
        resolver=_Resolver(),
        cache=cache,
        domain="database",
        **parts,
    )
    return orch, parts


def _arrive_at(state: GhostState, *, rollback_support: bool):
    """Reach *state* the way the app does; SUPERSEDED (reserved, never produced) is set directly."""
    orch, p = _make(rollback_support=rollback_support)
    p["validator"].result = {GhostState.REVIEW_REQUIRED: REVIEW, GhostState.BLOCKED: BLOCK}.get(state, VALID)
    report = orch.run()
    diff = report.differences[0]
    p["validator"].result = VALID
    if state == GhostState.RESOLVED:
        orch.apply_one(diff.id)
    elif state == GhostState.STILL_PRESENT:
        p["verifier"].resolved = False
        orch.apply_one(diff.id)
        p["verifier"].resolved = True
    elif state == GhostState.EXECUTION_FAILED:
        p["executor"].mode = "fail"
        orch.apply_one(diff.id)
        p["executor"].mode = "ok"
    elif state == GhostState.SUPERSEDED:
        diff.state = GhostState.SUPERSEDED
    assert diff.state == state
    return orch, p, report, diff


def _fire(event: str, orch, p, report, diff, *, auth: bool):
    """Fire one apply/rollback event; restores fakes afterwards. Returns the RollbackEvent if any."""
    original = report.resolutions[diff.id].proposed_change
    try:
        if event == "E3":
            p["verifier"].resolved = False
        elif event == "E4":
            p["executor"].mode = "raise"
        elif event == "E5":
            report.resolutions[diff.id].proposed_change = None
        elif event == "E6":
            p["validator"].result = BLOCK
        if event in ("E7", "E8"):
            return orch.rollback(diff.id)
        orch.apply_one(diff.id, human_authorized=auth)
        return None
    finally:
        p["verifier"].resolved = True
        p["executor"].mode = "ok"
        p["validator"].result = VALID
        report.resolutions[diff.id].proposed_change = original


STATES = list(GhostState)
EVENTS = ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"]
_APPLY_RESULT = {
    "E2": GhostState.RESOLVED,
    "E3": GhostState.STILL_PRESENT,
    "E4": GhostState.EXECUTION_FAILED,
    "E5": GhostState.REVIEW_REQUIRED,
    "E6": GhostState.BLOCKED,  # the validator's ghost_state
}


def _expected(state: GhostState, event: str, auth: bool) -> GhostState:
    """The transition table in design.md, encoded independently of the orchestrator."""
    if event == "E1":
        return GhostState.PENDING  # fresh report; the fake validator accepts
    if event in ("E7", "E8"):
        return state  # rollback never changes state
    if state == GhostState.PENDING or (state in GATED and auth):
        return _APPLY_RESULT[event]
    return state  # refused


def test_state_machine_has_56_cells():
    assert len(STATES) * len(EVENTS) == 56


@pytest.mark.parametrize("auth", [False, True], ids=["no-auth", "human-auth"])
@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("state", STATES, ids=[s.value for s in STATES])
def test_transition_table_cell(state, event, auth):
    orch, p, report, diff = _arrive_at(state, rollback_support=event == "E8")
    if event == "E1":
        fresh = orch.run().differences[0]
        assert fresh.id != diff.id
        assert fresh.state == _expected(state, event, auth)
        return
    rollback = _fire(event, orch, p, report, diff, auth=auth)
    assert diff.state == _expected(state, event, auth)
    if event == "E7":
        assert rollback.success is False
    if event == "E8":
        assert rollback.success is (state in (GhostState.RESOLVED, GhostState.STILL_PRESENT))


@settings(max_examples=150, deadline=None)
@given(
    st.sampled_from([VALID, REVIEW, BLOCK]),
    st.lists(
        st.tuples(st.sampled_from(["E2", "E3", "E4", "E5", "E6", "E8"]), st.booleans()),
        max_size=8,
    ),
)
def test_invariants_hold_over_random_event_sequences(initial, steps):
    orch, p = _make(rollback_support=True)
    p["validator"].result = initial
    report = orch.run()
    diff = report.differences[0]
    p["validator"].result = VALID

    # A validator result other than "valid" never leaves a ghost PENDING.
    if initial.status != "valid":
        assert diff.state != GhostState.PENDING

    for event, auth in steps:
        before = diff.state
        _fire(event, orch, p, report, diff, auth=auth)
        after = diff.state

        if before != GhostState.PENDING:
            assert after != GhostState.PENDING, "a ghost returned to PENDING"
        if event == "E8":
            assert after == before, "rollback changed state"
        if before in TERMINAL:
            assert after == before, f"terminal state {before} changed on {event}"
        if before in GATED and not auth:
            assert after == before, f"{before} changed without human authorization"

        persisted = orch.persistence.load(report.id)
        assert persisted is not None
        assert persisted.differences[0].state == after, "persisted state differs from memory"


# ===========================================================================
# 4. Redaction
# ===========================================================================

REDACTED = "***REDACTED***"
TRUNCATED = "***TRUNCATED***"

REQUIRED_SENSITIVE = [
    "authorization", "Authorization",
    "client_secret", "clientSecret", "CLIENTSECRET",
    "private_key", "privateKey", "privatekey",
]
SUBSTRING_TRAPS = ["monkey", "accessibility", "author"]
SENSITIVE_BASES = ["authorization", "secret", "token", "password", "key"]
SAFE_WORDS = [
    "monkey", "accessibility", "author", "keyboard", "tokenizer", "secretary",
    "passport", "user", "name", "email", "status", "created", "value", "count", "id",
]


def _join(parts: list[str], style: str) -> str:
    if style == "snake":
        return "_".join(parts)
    if style == "kebab":
        return "-".join(parts)
    if style == "upper_snake":
        return "_".join(parts).upper()
    if style == "camel":
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    if style == "pascal":
        return "".join(p.capitalize() for p in parts)
    if style == "header":
        return "-".join(p.capitalize() for p in parts)
    raise ValueError(style)


_STYLES = st.sampled_from(["snake", "kebab", "upper_snake", "camel", "pascal", "header"])


@st.composite
def _sensitive_key(draw):
    prefix = draw(st.lists(st.sampled_from(SAFE_WORDS), max_size=2))
    suffix = draw(st.lists(st.sampled_from(SAFE_WORDS), max_size=2))
    base = draw(st.sampled_from(SENSITIVE_BASES))
    return _join(prefix + [base] + suffix, draw(_STYLES))


@st.composite
def _safe_key(draw):
    parts = draw(st.lists(st.sampled_from(SAFE_WORDS), min_size=1, max_size=3))
    return _join(parts, draw(_STYLES))


@pytest.mark.parametrize("key", REQUIRED_SENSITIVE)
def test_required_variants_redacted(key):
    assert redact_value({key: "s3cr3t"})[key] == REDACTED
    assert redact_headers({key: "s3cr3t"})[key] == REDACTED


@pytest.mark.parametrize("key", SUBSTRING_TRAPS)
def test_substring_traps_not_redacted(key):
    assert redact_value({key: "visible"})[key] == "visible"
    assert redact_headers({key: "visible"})[key] == "visible"


@given(_sensitive_key())
def test_generated_sensitive_keys_redacted(key):
    assert redact_value({key: "s3cr3t"})[key] == REDACTED, key
    assert redact_headers({key: "s3cr3t"})[key] == REDACTED, key


@given(_safe_key())
def test_generated_safe_keys_not_redacted(key):
    assert redact_value({key: "visible"})[key] == "visible", key


@given(st.integers(min_value=0, max_value=10), _sensitive_key())
def test_secret_never_survives_any_depth(depth, key):
    """Depth ≤ 5: value redacted. Deeper: subtree truncated. Never the raw secret."""
    doc: object = {key: "s3cr3t-value"}
    for _ in range(depth):
        doc = {"level": doc}
    out = repr(redact_value(doc))
    assert "s3cr3t-value" not in out
    assert (REDACTED in out) if depth <= 5 else (TRUNCATED in out)


# ===========================================================================
# 5. SSRF guard — tech.md blocked ranges
# ===========================================================================

BLOCKED_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)]
BLOCKED_V6 = [ipaddress.ip_network(n) for n in ("::1/128", "::/128", "fc00::/7", "fe80::/10", "ff00::/8")]


def _address_in(networks):
    return st.sampled_from(networks).flatmap(
        lambda net: st.integers(min_value=0, max_value=net.num_addresses - 1).map(lambda k: net[k])
    )


def _validate_with(addr) -> str:
    with patch.object(ssrf, "_resolve", return_value=[addr]):
        return ssrf.validate("https://pbt-host.example/path")


@given(_address_in(BLOCKED_V4 + BLOCKED_V6))
def test_every_blocked_address_refused(addr):
    with pytest.raises(ssrf.SSRFError):
        _validate_with(addr)


@given(_address_in(BLOCKED_V4))
def test_ipv4_mapped_ipv6_of_blocked_address_refused(addr):
    with pytest.raises(ssrf.SSRFError):
        _validate_with(ipaddress.IPv6Address(f"::ffff:{addr}"))


@given(
    st.integers(min_value=0, max_value=2**32 - 1)
    .map(ipaddress.IPv4Address)
    .filter(lambda a: a.is_global and not any(a in n for n in BLOCKED_V4))
)
def test_public_ipv4_allowed(addr):
    assert _validate_with(addr) == "https://pbt-host.example/path"
