"""
adapters/postgres/executor.py — Req 20

Applies one validated ChangeProposal (SQL statement) to Postgres.

Rules:
  · Statement timeout is set at the CONNECTION level via options, never as SET.
  · Exactly one parsed statement, inside a transaction.
  · No automatic rollback of applied changes — database domain does not implement
    RollbackSupport. The orchestrator checks isinstance(executor, RollbackSupport)
    before attempting rollback; this executor does not implement that protocol.
  · Raw exception text is never stored.

The core validator (SQLValidator in adapters/postgres/validator.py) validates
before the orchestrator calls execute(). The precheck here is defence-in-depth.
"""

from __future__ import annotations

from typing import Any, Callable

import psycopg
import sqlglot
import sqlglot.expressions as exp

from core.models import ChangeProposal, Difference, ExecutionResult, Resolution

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
    return psycopg.connect(
        dsn,
        connect_timeout=CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


class SQLExecutor:
    """
    Executor protocol implementation for the database domain.

    Does NOT implement RollbackSupport — database rollback is not supported.
    The orchestrator checks isinstance(executor, RollbackSupport) before
    attempting rollback; since SQLExecutor does not implement it, rollback
    is refused automatically without any domain-string check in core.
    """

    def __init__(self, dsn: str, connect: ConnectFn | None = None) -> None:
        self._dsn = dsn
        self._connect = connect or default_connect

    def execute(self, resolution: Resolution, difference: Difference) -> ExecutionResult:
        proposal = resolution.proposed_change
        if proposal is None:
            return self._fail(resolution, None, "NO_CHANGE")

        statement = proposal.payload.get("statement", "")
        if not statement:
            return self._fail(resolution, proposal, "NOT_SQL")

        precheck = self._precheck(statement)
        if precheck:
            return self._fail(resolution, proposal, precheck)

        try:
            conn = self._connect(self._dsn)
        except Exception as exc:
            return self._fail(resolution, proposal, "CONNECT_FAILED", exc)

        try:
            try:
                conn.execute(statement)
                conn.commit()
            except psycopg.errors.QueryCanceled as exc:
                conn.rollback()
                return self._fail(resolution, proposal, "STATEMENT_TIMEOUT", exc)
            except Exception as exc:
                conn.rollback()
                return self._fail(resolution, proposal, "EXECUTION_ERROR", exc)
        finally:
            conn.close()

        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=True,
            applied_change=proposal,
        )

    @staticmethod
    def _precheck(statement: str) -> str:
        try:
            parsed = [s for s in sqlglot.parse(statement, dialect="postgres") if s is not None]
        except Exception:
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
        proposal: ChangeProposal | None,
        code: str,
        exc: BaseException | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            difference_id=resolution.difference_id,
            success=False,
            applied_change=proposal,
            error_code=code,
            error_class=type(exc).__name__ if exc is not None else "",
            error_summary=ERROR_SUMMARIES.get(code, code)[:200],
        )
