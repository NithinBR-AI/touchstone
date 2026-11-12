"""
test_executor_pg.py — Task 2.11, Req 20

Unit tests with a fake connection. Real-Postgres execution is covered by
test_sql_execution.py (task 2.14), which needs a reachable database.
"""

from __future__ import annotations

import psycopg
import pytest

import adapters.postgres.executor as executor_mod
from adapters.postgres.executor import ERROR_SUMMARIES, SQLExecutor
from core.models import Difference, FilePatch, Resolution, SQLChange

GOOD_SQL = "CREATE INDEX ix_users_email ON users (email)"


class FakeConn:
    def __init__(self, raise_on_execute: BaseException | None = None) -> None:
        self.raise_on_execute = raise_on_execute
        self.executed: list[str] = []
        self.committed = self.rolled_back = self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _res(change) -> Resolution:
    return Resolution(
        difference_id="d1", difference_signature="s1", proposed_change=change
    )


def _diff_stub() -> Difference:
    return Difference(
        id="d1",
        signature="s1",
        domain="database",
        kind="INDEX_MISSING",
        target="ix_users_email",
    )


def _sql(statement: str = GOOD_SQL) -> SQLChange:
    return SQLChange(
        statement=statement, operation="CREATE_INDEX", target="ix_users_email"
    )


def test_success_commits_and_closes() -> None:
    conn = FakeConn()
    result = SQLExecutor("dsn", connect=lambda _: conn).execute(
        _res(_sql()), _diff_stub()
    )
    assert result.success
    assert conn.executed == [GOOD_SQL]
    assert conn.committed and conn.closed and not conn.rolled_back
    assert result.applied_change.statement == GOOD_SQL
    assert result.rollback_bytes == b""  # database domain: no rollback data


def test_execution_error_rolls_back_without_raw_text() -> None:
    conn = FakeConn(RuntimeError("password=hunter2 leaked in driver message"))
    result = SQLExecutor("dsn", connect=lambda _: conn).execute(
        _res(_sql()), _diff_stub()
    )
    assert not result.success
    assert conn.rolled_back and conn.closed and not conn.committed
    assert result.error_code == "EXECUTION_ERROR"
    assert result.error_class == "RuntimeError"
    assert result.error_summary == ERROR_SUMMARIES["EXECUTION_ERROR"]
    for value in (result.error_code, result.error_class, result.error_summary):
        assert "hunter2" not in value


def test_statement_timeout_is_classified() -> None:
    conn = FakeConn(psycopg.errors.QueryCanceled("canceling statement due to timeout"))
    result = SQLExecutor("dsn", connect=lambda _: conn).execute(
        _res(_sql()), _diff_stub()
    )
    assert result.error_code == "STATEMENT_TIMEOUT"
    assert conn.rolled_back


def test_connect_failure_is_recorded() -> None:
    def boom(_: str):
        raise psycopg.OperationalError("could not connect to host secret.example")

    result = SQLExecutor("dsn", connect=boom).execute(_res(_sql()), _diff_stub())
    assert result.error_code == "CONNECT_FAILED"
    assert "secret" not in result.error_summary


@pytest.mark.parametrize(
    "statement,code",
    [
        ("CREATE INDEX a ON t (x); CREATE INDEX b ON t (y)", "MULTI_STATEMENT"),
        ("SET statement_timeout = 0", "SET_FORBIDDEN"),
        ("SET LOCAL statement_timeout = 0", "SET_FORBIDDEN"),
    ],
)
def test_prechecks_refuse_without_connecting(statement: str, code: str) -> None:
    def must_not_connect(_: str):
        raise AssertionError("executor connected despite a failed precheck")

    result = SQLExecutor("dsn", connect=must_not_connect).execute(
        _res(_sql(statement)), _diff_stub()
    )
    assert result.error_code == code


def test_no_change_and_non_sql_change_refused() -> None:
    ex = SQLExecutor("dsn", connect=lambda _: FakeConn())
    assert ex.execute(_res(None), _diff_stub()).error_code == "NO_CHANGE"
    patch = FilePatch("f", "a", "b", b"", b"")
    assert ex.execute(_res(patch), _diff_stub()).error_code == "NOT_SQL"


def test_default_connect_sets_timeouts_at_connection_level(monkeypatch) -> None:
    captured: dict = {}

    def fake_connect(dsn, **kwargs):
        captured.update(kwargs, dsn=dsn)
        return FakeConn()

    monkeypatch.setattr(executor_mod.psycopg, "connect", fake_connect)
    SQLExecutor("postgresql://x").execute(_res(_sql()), _diff_stub())
    assert captured["options"] == "-c statement_timeout=30000"
    assert captured["connect_timeout"] == 10
