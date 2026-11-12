"""
test_sql_execution.py — Task 2.14, Req 17

Real Postgres SQL execution tests. Skipped automatically when DATABASE_URL
is not set. Run after seeding the demo DB:

    docker compose -f fixtures/docker-compose.yml up -d
    python fixtures/seed.py postgresql://touchstone:touchstone@localhost:5432/touchstone_demo
    DATABASE_URL=postgresql://touchstone:touchstone@localhost:5432/touchstone_demo pytest tests/test_sql_execution.py -v
"""

from __future__ import annotations

import os

import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv()

from adapters.postgres.executor import STATEMENT_TIMEOUT_MS, SQLExecutor
from core.models import Difference, GhostState, Resolution, SQLChange

DB_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="DATABASE_URL not set — skipping real Postgres tests",
)


def _diff(kind: str, target: str, context: dict) -> Difference:
    return Difference(kind=kind, target=target, domain="database", context=context)


def _res(diff: Difference, change: SQLChange) -> Resolution:
    return Resolution(
        difference_id=diff.id,
        difference_signature=diff.signature or "",
        proposed_change=change,
        prompt_sha256="test",
    )


class TestSQLExecution:
    """SQL execution tests against real Postgres."""

    def test_create_index_executes_and_commits(self):
        """CREATE INDEX statement executes, commits, and can be verified."""
        ex = SQLExecutor(DB_URL)
        diff = _diff("INDEX_MISSING", "ix_users_email_exec_test", {})
        change = SQLChange(
            statement="CREATE INDEX IF NOT EXISTS ix_users_email_exec_test ON users (email)",
            operation="CREATE_INDEX",
            target="ix_users_email_exec_test",
        )
        result = ex.execute(_res(diff, change), diff)
        assert result.success, f"Expected success, got error_code={result.error_code}"
        assert result.applied_change is not None

        # Clean up
        conn = psycopg.connect(DB_URL)
        conn.execute("DROP INDEX IF EXISTS ix_users_email_exec_test")
        conn.commit()
        conn.close()

    def test_alter_column_type_widens_successfully(self):
        """ALTER TABLE … TYPE executes successfully for a widening change."""
        ex = SQLExecutor(DB_URL)
        diff = _diff(
            "TYPE_CHANGED",
            "users.email",
            {
                "original_actual_type": "varchar(100)",
                "table": "users",
                "column": "email",
            },
        )
        change = SQLChange(
            statement="ALTER TABLE users ALTER COLUMN email TYPE varchar(255)",
            operation="ALTER_COLUMN_TYPE",
            target="users.email",
        )
        result = ex.execute(_res(diff, change), diff)
        assert result.success, f"Expected success, got error_code={result.error_code}"

        # Verify type changed in DB
        conn = psycopg.connect(DB_URL)
        row = conn.execute(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name='users' AND column_name='email'"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] == 255

    def test_statement_timeout_is_enforced(self):
        """A long-running statement is cancelled by the connection-level timeout."""

        def fast_timeout_connect(dsn):
            return psycopg.connect(
                dsn, connect_timeout=10, options="-c statement_timeout=1"
            )

        ex = SQLExecutor(DB_URL, connect=fast_timeout_connect)
        diff = _diff("INDEX_MISSING", "ix_test_slow", {})
        change = SQLChange(
            statement="SELECT pg_sleep(10)",
            operation="CREATE_INDEX",
            target="ix_test_slow",
        )
        result = ex.execute(_res(diff, change), diff)
        assert not result.success
        assert result.error_code == "STATEMENT_TIMEOUT"

    def test_connect_failure_recorded_without_raw_text(self):
        """Connection failure stores error_code, not raw exception text."""
        ex = SQLExecutor("postgresql://bad:bad@localhost:9999/nonexistent")
        diff = _diff("INDEX_MISSING", "ix_test", {})
        change = SQLChange(
            statement="SELECT 1", operation="CREATE_INDEX", target="ix_test"
        )
        result = ex.execute(_res(diff, change), diff)
        assert not result.success
        assert result.error_code == "CONNECT_FAILED"
        assert result.error_class != ""

    def test_set_statement_refused_without_connecting(self):
        """SET statement is refused at precheck — no DB connection needed."""
        ex = SQLExecutor(DB_URL)
        diff = _diff("INDEX_MISSING", "ix_test", {})
        change = SQLChange(
            statement="SET statement_timeout = 0",
            operation="CREATE_INDEX",
            target="ix_test",
        )
        result = ex.execute(_res(diff, change), diff)
        assert not result.success
        assert result.error_code == "SET_FORBIDDEN"
