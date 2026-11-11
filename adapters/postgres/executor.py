"""
adapters/postgres/executor.py — Req 20

Applies one validated SQLChange to Postgres.

Rules (tech.md / Req 20):
  · Statement timeout is set at the CONNECTION level via options, never as a
    SET statement:  psycopg.connect(dsn, options="-c statement_timeout=30000")
  · Exactly one parsed statement, inside a transaction. Commit on success,
    rollback on error.
  · No automatic rollback of applied changes and no inverse statement is
    generated. The originally observed state lives in Difference.context and
    the executed SQL lives in ExecutionResult.applied_change.
  · Raw exception text is never stored. Errors are recorded as a code, the
    exception class name, and a summary taken from ERROR_SUMMARIES.

The orchestrator validates before calling execute(). The single-statement and
SET checks here are defence in depth, not the safety boundary.
"""

from __future__ import annotations

from typing import Any, Callable

import psycopg
import sqlglot
import sqlglot.expressions as exp

from core.models import ExecutionResult, Resolution, SQLChange

CONNECT_TIMEOUT_S = 10
STATEMENT_TIMEOUT_MS = 30000

ERROR_SUMMARIES: dict[str, str] = {
    "NO_CHANGE": "Resolution has no proposed change; nothing was executed.",
    "NOT_SQL": "Proposed change is not a SQL change; the database executor refused it.",
    "PARSE_ERROR": "Statement could not be parsed; nothing was executed.",
    "MULTI_STATEMENT": "Expected exactly one statement; nothing was executed.",
    "SET_FORBIDDEN": "SET statements are forbidden; timeouts are set on the connection.",
    "CONNECT_FAILED": "Could not connect to the database; nothing was executed.",
    "STATEMENT_TIMEOUT": "Statement exceeded the 30s timeout; the transaction was rolled back.",
    "EXECUTION_ERROR": "The database rejected the statement; the transaction was rolled back.",
}

ConnectFn = Callable[[str], Any]


def default_connect(dsn: str) -> psycopg.Connection:
    """Open a connection with connect and statement timeouts at connection level."""
    return psycopg.connect(
        dsn,
        connect_timeout=CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


class SQLExecutor:
    """Executor protocol implementation for the database domain."""

    def __init__(self, dsn: str, connect: ConnectFn | None = None) -> None:
        self._dsn = dsn
        self._connect = connect or default_connect

    def execute(self, resolution: Resolution) -> ExecutionResult:
        change = resolution.proposed_change
        if change is None:
            return self._fail(resolution, None, "NO_CHANGE")
        if not isinstance(change, SQLChange):
            return self._fail(resolution, change, "NOT_SQL")

        precheck = self._precheck(change.statement)
        if precheck:
            return self._fail(resolution, change, precheck)

        try:
            conn = self._connect(self._dsn)
        except Exception as exc:  # noqa: BLE001 — recorded as class name only
            return self._fail(resolution, change, "CONNECT_FAILED", exc)

        try:
            try:
                conn.execute(change.statement)
                conn.commit()
            except psycopg.errors.QueryCanceled as exc:
                conn.rollback()
                return self._fail(resolution, change, "STATEMENT_TIMEOUT", exc)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                return self._fail(resolution, change, "EXECUTION_ERROR", exc)
        finally:
            conn.close()

        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=True,
            applied_change=change,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _precheck(statement: str) -> str:
        try:
            parsed = [s for s in sqlglot.parse(statement, dialect="postgres") if s is not None]
        except Exception:  # noqa: BLE001
            return "PARSE_ERROR"
        if len(parsed) != 1:
            return "MULTI_STATEMENT"
        stmt = parsed[0]
        if isinstance(stmt, exp.Set) or (
            isinstance(stmt, exp.Command) and str(stmt.this).upper().startswith("SET")
        ):
            return "SET_FORBIDDEN"
        return ""

    @staticmethod
    def _fail(
        resolution: Resolution,
        change: Any,
        code: str,
        exc: BaseException | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=False,
            applied_change=change,
            error_code=code,
            error_class=type(exc).__name__ if exc is not None else "",
            error_summary=ERROR_SUMMARIES[code][:200],
        )
