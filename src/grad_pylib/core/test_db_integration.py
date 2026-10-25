import inspect
import logging
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from string.templatelib import Template
from typing import Any

import pymssql
import pyodbc
import pytest
from sqlalchemy import Column, Executable, Integer, MetaData, Table, create_engine, event, select, text, tstring
from sqlalchemy.engine import Connection, CursorResult, Engine, ExceptionContext
from sqlalchemy.exc import ArgumentError, DataError, DBAPIError, ResourceClosedError
from sqlalchemy.orm import Session, sessionmaker

from grad_pylib.core import db as db_module
from grad_pylib.core import multiquery as multiquery_module
from grad_pylib.core.db import (
    SqlServerErrorType,
    _sqlalchemy_cursor_errors,
    describe_disconnect,
    install_connection_reset,
    install_mssql_batch_support,
    install_terminated_session_detection,
    is_disconnect_error,
    is_retryable_disconnect,
    parse_mssql_error,
    retry_on_disconnect,
    retry_on_transient_conflict,
)
from grad_pylib.core.multiquery import execute_script_batch, read_all_result_sets
from grad_pylib.testing import ManagedE2eDatabases


@pytest.fixture(scope="module")
def mssql_error_engine(mssql_databases: ManagedE2eDatabases) -> Engine:
    with mssql_databases.app_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE dbo.parse_mssql_error_parent (
                id int NOT NULL PRIMARY KEY
            )
        """))
        connection.execute(text("""
            CREATE TABLE dbo.parse_mssql_error_values (
                id int IDENTITY(1, 1) NOT NULL PRIMARY KEY,
                short_value varchar(4) NOT NULL,
                integer_value int NOT NULL,
                unique_value int NOT NULL UNIQUE,
                status varchar(8) NOT NULL
                    CONSTRAINT CK_parse_mssql_error_values_status CHECK (status IN ('valid', 'other')),
                parent_id int NOT NULL
                    CONSTRAINT FK_parse_mssql_error_values_parent
                    REFERENCES dbo.parse_mssql_error_parent (id)
            )
        """))
        connection.execute(text("INSERT INTO dbo.parse_mssql_error_parent (id) VALUES (1)"))
        connection.execute(text("""
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES ('good', 1, 1, 'valid', 1)
        """))
    return mssql_databases.app_engine


@pytest.mark.parametrize(
    ("statement", "expected_type", "expected_code"),
    [
        (
            """
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES ('too long', 1, 2, 'valid', 1)
            """,
            SqlServerErrorType.DATA_TRUNCATION,
            2628,
        ),
        (
            """
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES (NULL, 1, 3, 'valid', 1)
            """,
            SqlServerErrorType.NOT_NULL_VIOLATION,
            515,
        ),
        (
            """
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES ('good', 1, 1, 'valid', 1)
            """,
            SqlServerErrorType.DUPLICATE_KEY,
            2627,
        ),
        (
            """
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES ('good', 1, 4, 'invalid', 1)
            """,
            SqlServerErrorType.CHECK_CONSTRAINT_VIOLATION,
            547,
        ),
        (
            """
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES ('good', 1, 5, 'valid', 2)
            """,
            SqlServerErrorType.FOREIGN_KEY_VIOLATION,
            547,
        ),
        (
            """
            INSERT INTO dbo.parse_mssql_error_values
                (short_value, integer_value, unique_value, status, parent_id)
            VALUES ('good', 'not-an-int', 6, 'valid', 1)
            """,
            SqlServerErrorType.DATA_CONVERSION,
            245,
        ),
        ("SELECT CAST(1000 AS tinyint)", SqlServerErrorType.ARITHMETIC_OVERFLOW, 220),
        ("SELECT 1 / 0", SqlServerErrorType.DIVIDE_BY_ZERO, 8134),
        ("SELECT missing_column FROM dbo.parse_mssql_error_values", SqlServerErrorType.INVALID_COLUMN, 207),
        ("SELECT * FROM dbo.missing_parse_mssql_error_table", SqlServerErrorType.INVALID_OBJECT, 208),
    ],
)
def test_parse_mssql_error_classifies_native_sql_server_errors(
        mssql_error_engine: Engine,
        statement: str,
        expected_type: SqlServerErrorType,
        expected_code: int,
) -> None:
    with mssql_error_engine.connect() as connection, pytest.raises(DBAPIError) as raised:
        connection.execute(text(statement))

    parsed = parse_mssql_error(raised.value)

    assert parsed.error_type is expected_type
    assert parsed.native_code == expected_code


@pytest.fixture
def lost_connection_sessions(mssql_databases: ManagedE2eDatabases) -> Generator[sessionmaker[Session]]:
    engine = create_engine(mssql_databases.app_engine.url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS dbo.lost_connection_writes"))
        connection.execute(text("CREATE TABLE dbo.lost_connection_writes (id int NOT NULL PRIMARY KEY)"))
    yield sessionmaker(engine)
    engine.dispose()


def kill_connection(session: Session, admin_engine: Engine) -> None:
    """Make SQL Server drop the session's connection, as a network failure would."""
    kill_spid(session.execute(text("SELECT @@SPID")).scalar_one(), admin_engine)


def kill_spid(spid: int, admin_engine: Engine) -> None:
    with admin_engine.connect() as connection:
        # KILL cannot run inside a transaction.
        connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.execute(text(f"KILL {int(spid)}"))


def test_connection_lost_before_a_statement_is_retryable(
        mssql_databases: ManagedE2eDatabases,
        lost_connection_sessions: sessionmaker[Session],
) -> None:
    with lost_connection_sessions() as session:
        kill_connection(session, mssql_databases.app_engine)
        with pytest.raises(DBAPIError) as raised:
            session.execute(text("INSERT INTO dbo.lost_connection_writes (id) VALUES (1)"))

    assert is_disconnect_error(raised.value)
    assert is_retryable_disconnect(raised.value)
    assert describe_disconnect(raised.value)["during_commit"] is False


def test_connection_lost_during_commit_is_not_retryable(
        mssql_databases: ManagedE2eDatabases,
        lost_connection_sessions: sessionmaker[Session],
) -> None:
    with lost_connection_sessions() as session:
        session.execute(text("INSERT INTO dbo.lost_connection_writes (id) VALUES (1)"))
        kill_connection(session, mssql_databases.app_engine)
        with pytest.raises(DBAPIError) as raised:
            session.commit()

    # Relies on pyodbc naming the failing ODBC function (SQLEndTran) in the driver message.
    assert is_disconnect_error(raised.value)
    assert not is_retryable_disconnect(raised.value)
    assert describe_disconnect(raised.value)["during_commit"] is True


def test_retry_on_transient_conflict_reconnects_after_a_lost_connection(
        mssql_databases: ManagedE2eDatabases,
        lost_connection_sessions: sessionmaker[Session],
) -> None:
    attempts: list[None] = []

    @retry_on_transient_conflict
    def insert_row(session: Session) -> None:
        attempts.append(None)
        if len(attempts) == 1:
            kill_connection(session, mssql_databases.app_engine)
        session.execute(text("INSERT INTO dbo.lost_connection_writes (id) VALUES (1)"))

    with lost_connection_sessions() as session:
        insert_row(session)
        rows = session.execute(text("SELECT id FROM dbo.lost_connection_writes")).scalars().all()

    assert len(attempts) == 2
    assert rows == [1]


@pytest.fixture
def contended_sessions(mssql_databases: ManagedE2eDatabases) -> Generator[sessionmaker[Session]]:
    engine = create_engine(mssql_databases.app_engine.url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS dbo.contended_counters"))
        connection.execute(text("DROP TABLE IF EXISTS dbo.contended_writes"))
        connection.execute(text("CREATE TABLE dbo.contended_counters (id int NOT NULL PRIMARY KEY, v int NOT NULL)"))
        connection.execute(text("INSERT INTO dbo.contended_counters (id, v) VALUES (1, 0), (2, 0)"))
        connection.execute(text("CREATE TABLE dbo.contended_writes (attempt int NOT NULL PRIMARY KEY)"))
    yield sessionmaker(engine)
    engine.dispose()


def deadlock_against(session: Session, competitor_engine: Engine) -> None:
    """Make the session the victim of a deadlock with a higher-priority competing transaction."""
    competitor_ready = threading.Event()

    def competitor() -> None:
        with competitor_engine.connect() as connection:
            connection.execute(text("SET DEADLOCK_PRIORITY HIGH"))
            connection.execute(text("UPDATE dbo.contended_counters SET v = v + 1 WHERE id = 2"))
            competitor_ready.set()
            connection.execute(text("UPDATE dbo.contended_counters SET v = v + 1 WHERE id = 1"))
            connection.commit()

    session.execute(text("UPDATE dbo.contended_counters SET v = v + 1 WHERE id = 1"))
    thread = threading.Thread(target=competitor)
    thread.start()
    try:
        assert competitor_ready.wait(10)
        # Let the competitor block on row 1 before this session requests row 2.
        time.sleep(0.3)
        session.execute(text("UPDATE dbo.contended_counters SET v = v + 1 WHERE id = 2"))
    finally:
        thread.join(30)


def test_retry_on_transient_conflict_commits_only_the_attempt_after_a_deadlock(
        mssql_databases: ManagedE2eDatabases,
        contended_sessions: sessionmaker[Session],
) -> None:
    attempts: list[None] = []

    @retry_on_transient_conflict
    def write(session: Session) -> None:
        attempts.append(None)
        session.execute(text("INSERT INTO dbo.contended_writes (attempt) VALUES (:attempt)"), {"attempt": len(attempts)})
        if len(attempts) == 1:
            deadlock_against(session, mssql_databases.app_engine)

    with contended_sessions() as session:
        write(session)
        assert not session.in_transaction()
        writes = session.execute(text("SELECT attempt FROM dbo.contended_writes")).scalars().all()
        counters = session.execute(text("SELECT v FROM dbo.contended_counters ORDER BY id")).scalars().all()

    assert len(attempts) == 2
    assert writes == [2]
    # Only the competitor's increments survive; the deadlock victim's were rolled back.
    assert counters == [1, 1]



@pytest.fixture
def single_connection_engine(mssql_databases: ManagedE2eDatabases) -> Generator[Engine]:
    engine = create_engine(mssql_databases.app_engine.url, pool_size=1, max_overflow=0)
    install_connection_reset(engine)
    yield engine
    engine.dispose()


def _session_state(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        spid, nocount = connection.execute(text("SELECT @@SPID, @@OPTIONS & 512")).one()
    return spid, nocount


def test_connection_reset_clears_state_left_by_a_failed_batch(single_connection_engine: Engine) -> None:
    with single_connection_engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        with pytest.raises(DBAPIError):
            # A conversion error aborts the batch before it can restore NOCOUNT.
            connection.execute(text("SET NOCOUNT ON; SELECT CAST('x' AS int); SET NOCOUNT OFF;"))

    assert _session_state(single_connection_engine) == (spid, 0)


def test_connection_reset_skips_clean_returns(single_connection_engine: Engine) -> None:
    with single_connection_engine.connect() as connection:
        spid = connection.execute(text("SET NOCOUNT ON; SELECT @@SPID")).scalar_one()

    # Clean returns are not reset, so batches must restore their own state.
    assert _session_state(single_connection_engine) == (spid, 512)


# Textual batch regressions for the `mssql_skip_rowcounts` option (DB-IMPLEMENTATION.md, R1). Without it, SQLAlchemy
# builds a result from the batch's first result and closes the cursor, discarding the remaining results unread.
_SKIP_ROWCOUNTS = {"mssql_skip_rowcounts": True}
# Large enough that SQL Server is still sending it when the session is killed.
_LARGE_SELECT = "SELECT a.object_id FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b"
_batch_writes = Table("batch_writes", MetaData(), Column("id", Integer, primary_key=True, autoincrement=False), schema="dbo")


@pytest.fixture(scope="module")
def batch_engine(mssql_databases: ManagedE2eDatabases) -> Engine:
    with mssql_databases.app_engine.begin() as connection:
        connection.execute(text("CREATE TABLE dbo.batch_writes (id int NOT NULL PRIMARY KEY)"))
    return mssql_databases.app_engine


@pytest.fixture
def batch_connection(batch_engine: Engine) -> Generator[Connection]:
    """Run batches with NOCOUNT OFF in a transaction that is rolled back afterwards."""
    with batch_engine.connect() as connection, connection.begin() as transaction:
        assert connection.execute(text("SELECT @@OPTIONS & 512")).scalar_one() == 0
        yield connection
        transaction.rollback()


def record_cursor_executions(connection: Connection) -> list[str]:
    statements: list[str] = []

    def record(_conn: Connection, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    return statements


def batch_rows(connection: Connection) -> list[int]:
    return list(connection.execute(text("SELECT id FROM dbo.batch_writes ORDER BY id")).scalars())


@pytest.mark.parametrize("execution_options", [{}, {"mssql_skip_rowcounts": False}], ids=["absent", "false"])
def test_dml_before_a_select_hides_the_select_without_the_option(
        batch_connection: Connection,
        execution_options: dict[str, Any],
) -> None:
    result = batch_connection.execute(
        text("INSERT INTO dbo.batch_writes (id) VALUES (1); SELECT id FROM dbo.batch_writes"),
        execution_options=execution_options,
    )

    assert not result.returns_rows
    with pytest.raises(ResourceClosedError):
        result.all()


def _batch_ending_with(select_sql: str, kind: str) -> Executable:
    insert_sql = "INSERT INTO dbo.batch_writes (id) VALUES (1); UPDATE dbo.batch_writes SET id = id WHERE id = 0; "
    if kind == "tstring":
        return tstring(Template(insert_sql + select_sql))
    statement = text(insert_sql + select_sql)
    return statement.columns(id=Integer) if kind == "declared-columns" else statement


@pytest.mark.parametrize("kind", ["text", "tstring", "declared-columns"])
@pytest.mark.parametrize("option_level", ["statement", "execute", "connection"])
@pytest.mark.parametrize(("where", "expected"), [("", [1]), ("WHERE id = 0", [])], ids=["rows", "empty"])
def test_opted_in_dml_before_a_select_returns_the_select(
        batch_connection: Connection,
        kind: str,
        option_level: str,
        where: str,
        expected: list[int],
) -> None:
    statement = _batch_ending_with(f"SELECT id FROM dbo.batch_writes {where}", kind)
    statements = record_cursor_executions(batch_connection)
    if option_level == "statement":
        result = batch_connection.execute(statement.execution_options(**_SKIP_ROWCOUNTS))
    elif option_level == "execute":
        result = batch_connection.execute(statement, execution_options=_SKIP_ROWCOUNTS)
    else:
        result = batch_connection.execution_options(**_SKIP_ROWCOUNTS).execute(statement)

    assert list(result.keys()) == ["id"]
    assert result.scalars().all() == expected
    # The batch is the only statement sent, and it leaves NOCOUNT alone.
    assert len(statements) == 1
    assert batch_connection.execute(text("SELECT @@OPTIONS & 512")).scalar_one() == 0


def test_opted_in_driver_sql_returns_the_select_after_dml(batch_connection: Connection) -> None:
    result = batch_connection.exec_driver_sql(
        "INSERT INTO dbo.batch_writes (id) VALUES (?); SELECT id FROM dbo.batch_writes",
        (1,),
        execution_options=_SKIP_ROWCOUNTS,
    )

    assert result.scalars().all() == [1]


def test_opted_in_batches_stop_at_a_leading_empty_select(batch_connection: Connection) -> None:
    """An empty rowset is a real result, not a row count to skip."""
    result = batch_connection.execute(
        text("SELECT id FROM dbo.batch_writes WHERE 1 = 0; INSERT INTO dbo.batch_writes (id) VALUES (1); SELECT 2 AS b"),
        execution_options=_SKIP_ROWCOUNTS,
    )

    assert list(result.keys()) == ["id"]
    assert result.all() == []


def test_opted_in_session_batches_return_the_select(batch_engine: Engine) -> None:
    with Session(batch_engine) as session:
        rows = session.execute(
            text("INSERT INTO dbo.batch_writes (id) VALUES (1); SELECT id FROM dbo.batch_writes"),
            execution_options=_SKIP_ROWCOUNTS,
        ).scalars().all()
        session.rollback()

    assert rows == [1]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("UPDATE dbo.batch_writes SET id = id WHERE id = 0", []),
        (
            "INSERT INTO dbo.batch_writes (id) VALUES (1); UPDATE dbo.batch_writes SET id = id WHERE id = 0; "
            "INSERT INTO dbo.batch_writes (id) VALUES (2)",
            [1, 2],
        ),
    ],
    ids=["zero-rows", "several-statements"],
)
def test_opted_in_dml_only_batches_complete_without_a_result(
        batch_engine: Engine,
        batch_connection: Connection,
        statement: str,
        expected: list[int],
) -> None:
    more_results: list[bool] = []

    def check_exhausted(_conn: Connection, cursor: Any, *_args: Any) -> None:
        more_results.append(bool(cursor.nextset()))

    # Engine listeners run in registration order, so this sees the cursor after navigation.
    event.listen(batch_engine, "after_cursor_execute", check_exhausted)
    try:
        result = batch_connection.execute(text(statement), execution_options=_SKIP_ROWCOUNTS)
    finally:
        event.remove(batch_engine, "after_cursor_execute", check_exhausted)

    assert not result.returns_rows
    assert more_results == [False]
    assert batch_rows(batch_connection) == expected


_DML_ONLY_BATCH_WITH_A_FAILING_STATEMENT = (
    "INSERT INTO dbo.batch_writes (id) VALUES (1); "
    "INSERT INTO dbo.batch_writes (id) VALUES (1 / 0); "
    "INSERT INTO dbo.batch_writes (id) VALUES (3)"
)


def test_dml_only_batches_lose_later_errors_without_the_option(batch_connection: Connection) -> None:
    batch_connection.execute(text(_DML_ONLY_BATCH_WITH_A_FAILING_STATEMENT))

    assert batch_rows(batch_connection) == [1, 3]


@pytest.mark.parametrize(
    "statement",
    [
        _DML_ONLY_BATCH_WITH_A_FAILING_STATEMENT,
        "INSERT INTO dbo.batch_writes (id) VALUES (1); UPDATE dbo.batch_writes SET id = id WHERE id = 0; "
        "INSERT INTO dbo.batch_writes (id) VALUES (1 / 0)",
    ],
    ids=["middle", "last"],
)
def test_opted_in_dml_only_batches_raise_errors_from_later_statements(
        batch_connection: Connection,
        statement: str,
) -> None:
    with pytest.raises(DataError) as raised:
        batch_connection.execute(text(statement), execution_options=_SKIP_ROWCOUNTS)

    assert parse_mssql_error(raised.value).error_type is SqlServerErrorType.DIVIDE_BY_ZERO
    assert raised.value.__cause__ is raised.value.orig


@pytest.mark.parametrize(
    ("statement", "parameters", "message"),
    [
        (select(_batch_writes.c.id), None, "textual statements"),
        (_batch_writes.insert().values(id=1), None, "textual statements"),
        (text("INSERT INTO dbo.batch_writes (id) VALUES (:id)"), [{"id": 1}, {"id": 2}], "executemany"),
    ],
    ids=["core-select", "core-insert", "executemany"],
)
def test_unsupported_opted_in_statements_are_rejected_before_execution(
        batch_engine: Engine,
        statement: Executable,
        parameters: list[dict[str, Any]] | None,
        message: str,
) -> None:
    with batch_engine.connect() as connection:
        statements = record_cursor_executions(connection)
        with pytest.raises(ArgumentError, match=message):
            connection.execute(statement, parameters, execution_options=_SKIP_ROWCOUNTS)

        assert statements == []
        assert not connection.in_transaction()
        assert batch_rows(connection) == []


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_invalid_option_values_are_rejected_before_execution(batch_engine: Engine, value: object) -> None:
    with batch_engine.connect() as connection:
        statements = record_cursor_executions(connection)
        with pytest.raises(ArgumentError, match="must be True or False"):
            connection.execute(
                text("INSERT INTO dbo.batch_writes (id) VALUES (1)"),
                execution_options={"mssql_skip_rowcounts": value},
            )

        assert statements == []
        assert batch_rows(connection) == []


def test_opted_in_orm_statements_are_rejected_before_execution(batch_engine: Engine) -> None:
    with Session(batch_engine) as session, pytest.raises(ArgumentError, match="textual statements"):
        session.execute(select(_batch_writes.c.id), execution_options=_SKIP_ROWCOUNTS)


def test_opted_in_driver_sql_executemany_is_rejected_before_execution(batch_connection: Connection) -> None:
    with pytest.raises(ArgumentError, match="executemany"):
        batch_connection.exec_driver_sql(
            "INSERT INTO dbo.batch_writes (id) VALUES (?)", [(1,), (2,)], execution_options=_SKIP_ROWCOUNTS,
        )

    assert batch_rows(batch_connection) == []


@pytest.mark.parametrize("execution_options", [{}, _SKIP_ROWCOUNTS], ids=["default", "opted-in"])
def test_consuming_one_rowset_does_not_reach_errors_later_in_the_batch(
        batch_connection: Connection,
        execution_options: dict[str, Any],
) -> None:
    """The option stops at the first rowset; work after it still needs complete traversal."""
    result = batch_connection.execute(text("SELECT 1 AS a; SELECT 1 / 0 AS b"), execution_options=execution_options)

    assert result.scalars().all() == [1]


def test_pyodbc_reports_a_session_killed_mid_batch_as_exhaustion_and_a_successful_commit(
        mssql_databases: ManagedE2eDatabases,
        batch_engine: Engine,
) -> None:
    """Pins a pyodbc 5.3.0/ODBC Driver 18 hazard that complete traversal alone cannot detect.

    After SQL Server kills a session that is still sending results, `nextset()` reports
    exhaustion and `commit()` succeeds although SQL Server rolled the transaction back. Only the
    connection's next statement fails, as a disconnect.
    """
    with batch_engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        connection.execute(text("INSERT INTO dbo.batch_writes (id) VALUES (1)"))
        result = connection.execute(text(f"{_LARGE_SELECT}; SELECT 2 AS b"))
        assert isinstance(result, CursorResult)
        cursor = result.cursor
        assert cursor is not None
        assert cursor.fetchone() is not None
        kill_spid(spid, mssql_databases.app_engine)

        assert not cursor.nextset()
        result.close()
        connection.commit()
        with pytest.raises(DBAPIError) as raised:
            connection.execute(text("SELECT 1"))

    assert is_disconnect_error(raised.value)
    with batch_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM dbo.batch_writes")).scalar_one() == 0


# `_sqlalchemy_cursor_errors` compatibility (DB-IMPLEMENTATION.md, R3). It calls a private SQLAlchemy method, so
# rerun these after every SQLAlchemy or driver upgrade.
_FAILS_ON_EXECUTE = "SET NOCOUNT ON; SELECT CAST('x' AS int) AS b; SET NOCOUNT OFF"
_FAILS_AFTER_A_ROWSET = "SET NOCOUNT ON; SELECT 1 AS a; SELECT CAST('x' AS int) AS b; SET NOCOUNT OFF"


@dataclass(frozen=True, slots=True)
class HandledError:
    """A `handle_error` dispatch; SQLAlchemy 2.1.5 never sets `ExceptionContext.cursor`, so it is not recorded."""
    original: BaseException
    wrapped: BaseException | None
    is_disconnect: bool
    execution_context: object
    statement: str | None


@dataclass
class ListenedEngine:
    """Engine with the runtime's error listeners that records `handle_error` dispatches and log events."""
    engine: Engine
    handled: list[HandledError] = field(default_factory=list)
    log_events: list[str] = field(default_factory=list)

    def record(self, context: ExceptionContext) -> None:
        self.handled.append(HandledError(
            context.original_exception,
            context.sqlalchemy_exception,
            context.is_disconnect,
            context.execution_context,
            context.statement,
        ))

    def info(self, event_name: str, /, **_fields: object) -> None:
        self.log_events.append(event_name)

    warning = info


@pytest.fixture
def listened_engine(mssql_databases: ManagedE2eDatabases, monkeypatch: pytest.MonkeyPatch) -> Generator[ListenedEngine]:
    engine = create_engine(mssql_databases.app_engine.url, pool_size=1, max_overflow=0)
    install_terminated_session_detection(engine)
    install_connection_reset(engine)
    install_mssql_batch_support(engine)
    db_module._install_disconnect_logging(engine)
    listened = ListenedEngine(engine)
    event.listen(engine, "handle_error", listened.record)
    monkeypatch.setattr(db_module, "_logger", listened)
    yield listened
    engine.dispose()


def record_connects(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[None]:
    connects: list[None] = []
    real_connect = engine.dialect.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        connects.append(None)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(engine.dialect, "connect", connect)
    return connects


def original_error(error: DBAPIError) -> BaseException:
    assert error.orig is not None
    return error.orig


def assert_wraps_conversion_error(error: DBAPIError) -> None:
    assert isinstance(error, DataError)
    assert isinstance(error.orig, pyodbc.DataError)
    assert error.__cause__ is error.orig
    assert parse_mssql_error(error).error_type is SqlServerErrorType.DATA_CONVERSION
    assert not error.connection_invalidated


def test_cursor_error_adapter_matches_the_private_sqlalchemy_api_it_calls() -> None:
    assert list(inspect.signature(Connection._handle_dbapi_exception).parameters) == [
        "self", "e", "statement", "parameters", "cursor", "context", "is_sub_exec",
    ]


def test_result_cursor_errors_reach_sqlalchemy_error_handling(listened_engine: ListenedEngine) -> None:
    with listened_engine.engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        result = connection.execute(text(_FAILS_AFTER_A_ROWSET))
        assert isinstance(result, CursorResult)
        cursor, context = result.cursor, result.context
        assert cursor is not None
        assert [tuple(row) for row in cursor.fetchall()] == [(1,)]

        with pytest.raises(DBAPIError) as raised, _sqlalchemy_cursor_errors(connection, cursor, context=context):
            cursor.nextset()
        result.close()
        assert connection.in_transaction()

    assert_wraps_conversion_error(raised.value)
    assert listened_engine.handled == [HandledError(original_error(raised.value), raised.value, False, context, None)]
    # The batch failed before restoring NOCOUNT; the error-only reset restored it on return to the pool.
    assert _session_state(listened_engine.engine) == (spid, 0)


@pytest.mark.parametrize("statement", [_FAILS_ON_EXECUTE, _FAILS_AFTER_A_ROWSET], ids=["execute", "nextset"])
def test_raw_cursor_errors_reach_sqlalchemy_error_handling(listened_engine: ListenedEngine, statement: str) -> None:
    with listened_engine.engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        cursor = connection.connection.cursor()

        with pytest.raises(DBAPIError) as raised, _sqlalchemy_cursor_errors(connection, cursor, statement=statement):
            cursor.execute(statement)
            while cursor.nextset():
                pass
        assert connection.in_transaction()

    assert_wraps_conversion_error(raised.value)
    assert raised.value.statement == statement
    assert listened_engine.handled == [HandledError(original_error(raised.value), raised.value, False, None, statement)]
    assert _session_state(listened_engine.engine) == (spid, 0)


def test_raw_cursor_disconnects_invalidate_without_reconnecting(
        mssql_databases: ManagedE2eDatabases,
        listened_engine: ListenedEngine,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = "SELECT 1"
    with listened_engine.engine.connect() as connection:
        kill_spid(connection.execute(text("SELECT @@SPID")).scalar_one(), mssql_databases.app_engine)
        cursor = connection.connection.cursor()
        connects = record_connects(listened_engine.engine, monkeypatch)

        with pytest.raises(DBAPIError) as raised, _sqlalchemy_cursor_errors(connection, cursor, statement=statement):
            cursor.execute(statement)
        assert connection.invalidated

    assert connects == []
    assert is_retryable_disconnect(raised.value)
    assert raised.value.__cause__ is raised.value.orig
    assert [handled.is_disconnect for handled in listened_engine.handled] == [True]
    assert listened_engine.log_events == ["db.disconnect"]
    with listened_engine.engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_script_batch_disconnects_invalidate_without_reconnecting_or_closing_the_cursor(
        mssql_databases: ManagedE2eDatabases,
        listened_engine: ListenedEngine,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(multiquery_module, "_logger", listened_engine)
    with listened_engine.engine.connect() as connection:
        kill_spid(connection.execute(text("SELECT @@SPID")).scalar_one(), mssql_databases.app_engine)
        connects = record_connects(listened_engine.engine, monkeypatch)

        with pytest.raises(DBAPIError) as raised:
            execute_script_batch(connection, "SELECT 1")
        assert connection.invalidated

    assert connects == []
    assert is_retryable_disconnect(raised.value)
    assert raised.value.statement == "SELECT 1"
    # No "db.script.cursor_close_failed": the invalidated connection's cursor is not closed again.
    assert listened_engine.log_events == ["db.disconnect"]


def test_result_cursor_disconnects_invalidate_without_reconnecting(
        listened_engine: ListenedEngine,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed session can read as exhaustion here, so the driver error is supplied."""
    driver_error = pyodbc.OperationalError(
        "08S01", "[08S01] [Microsoft][ODBC Driver 18 for SQL Server]Communication link failure (0) (SQLMoreResults)"
    )
    with listened_engine.engine.connect() as connection:
        result = connection.execute(text("SELECT 1 AS a; SELECT 2 AS b"))
        assert isinstance(result, CursorResult)
        cursor, context = result.cursor, result.context
        connects = record_connects(listened_engine.engine, monkeypatch)

        with pytest.raises(DBAPIError) as raised, _sqlalchemy_cursor_errors(connection, cursor, context=context):
            raise driver_error
        result.close()
        assert connection.invalidated

    assert connects == []
    assert raised.value.orig is driver_error
    assert raised.value.__cause__ is driver_error
    assert is_retryable_disconnect(raised.value)
    assert listened_engine.handled == [HandledError(driver_error, raised.value, True, context, None)]
    assert listened_engine.log_events == ["db.disconnect"]


def test_cursor_error_adapter_leaves_handled_and_non_driver_errors_alone(listened_engine: ListenedEngine) -> None:
    non_driver_error = ValueError("row mapping failed")
    with listened_engine.engine.connect() as connection:
        with pytest.raises(DataError) as handled, _sqlalchemy_cursor_errors(connection, None, statement="SELECT 1"):
            connection.execute(text("SELECT CAST('x' AS int)"))
        with pytest.raises(ValueError) as raised, _sqlalchemy_cursor_errors(connection, None, statement="SELECT 1"):
            raise non_driver_error
        assert not connection.invalidated

    assert raised.value is non_driver_error
    assert raised.value.__cause__ is None
    # SQLAlchemy dispatched its own error once; listeners also see non-driver errors, as during its fetches.
    assert [error.original for error in listened_engine.handled] == [handled.value.orig, non_driver_error]
    assert listened_engine.handled[1].wrapped is None


@pytest.fixture
def pymssql_engine(mssql_databases: ManagedE2eDatabases) -> Generator[Engine]:
    engine = create_engine(mssql_databases.app_engine.url.set(drivername="mssql+pymssql", query={}))
    yield engine
    engine.dispose()


def test_pymssql_raw_cursor_errors_reach_sqlalchemy_error_handling(pymssql_engine: Engine) -> None:
    handled: list[BaseException] = []

    def record(context: ExceptionContext) -> None:
        handled.append(context.original_exception)

    event.listen(pymssql_engine, "handle_error", record)
    with pymssql_engine.connect() as connection:
        cursor = connection.connection.cursor()
        with (
            pytest.raises(DBAPIError) as raised,
            _sqlalchemy_cursor_errors(connection, cursor, statement=_FAILS_AFTER_A_ROWSET),
        ):
            cursor.execute(_FAILS_AFTER_A_ROWSET)
            while cursor.nextset():
                pass

    assert isinstance(raised.value.orig, pymssql.Error)
    assert raised.value.__cause__ is raised.value.orig
    assert raised.value.statement == _FAILS_AFTER_A_ROWSET
    assert not raised.value.connection_invalidated
    assert handled == [raised.value.orig]


@pytest.mark.parametrize("driver_sql", [False, True], ids=["text", "driver-sql"])
def test_opted_in_batches_are_rejected_on_unsupported_drivers(
        batch_engine: Engine,
        pymssql_engine: Engine,
        driver_sql: bool,
) -> None:
    install_mssql_batch_support(pymssql_engine)
    statement = "INSERT INTO dbo.batch_writes (id) VALUES (1)"
    with pymssql_engine.connect() as connection:
        with pytest.raises(ArgumentError, match=r"requires an mssql\+pyodbc engine, not mssql\+pymssql"):
            if driver_sql:
                connection.exec_driver_sql(statement, execution_options=_SKIP_ROWCOUNTS)
            else:
                connection.execute(text(statement), execution_options=_SKIP_ROWCOUNTS)
        # Unopted-in work on the same engine is unaffected.
        assert connection.execute(text("SELECT COUNT(*) FROM dbo.batch_writes")).scalar_one() == 0


def test_a_session_the_server_terminates_is_a_retryable_disconnect(listened_engine: ListenedEngine) -> None:
    with listened_engine.engine.connect() as connection:
        with pytest.raises(DBAPIError) as raised:
            connection.execute(text("RAISERROR('fatal', 20, 1) WITH LOG"))
        assert connection.invalidated

    assert original_error(raised.value).args[0] == "HY000"
    assert is_retryable_disconnect(raised.value)
    assert [handled.is_disconnect for handled in listened_engine.handled] == [True]
    assert listened_engine.log_events == ["db.disconnect"]
    with listened_engine.engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_a_session_killed_during_a_fetch_is_a_retryable_disconnect(
        mssql_databases: ManagedE2eDatabases,
        listened_engine: ListenedEngine,
) -> None:
    with listened_engine.engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        result = connection.execute(text(_LARGE_SELECT))
        assert result.fetchone() is not None
        kill_spid(spid, mssql_databases.app_engine)

        with pytest.raises(DBAPIError) as raised:
            result.fetchall()
        assert connection.invalidated

    assert original_error(raised.value).args[0] == "HY000"
    assert "(SQLFetch)" in str(raised.value.orig)
    assert is_retryable_disconnect(raised.value)
    assert listened_engine.log_events == ["db.disconnect"]


def test_other_general_driver_errors_keep_the_connection(listened_engine: ListenedEngine) -> None:
    with listened_engine.engine.connect() as connection:
        result = connection.execute(text(_LARGE_SELECT))
        assert result.fetchone() is not None

        with pytest.raises(DBAPIError, match="Connection is busy") as raised:
            connection.execute(text("SELECT 1"))
        result.close()

        assert not connection.invalidated
        assert connection.execute(text("SELECT 2")).scalar_one() == 2

    assert original_error(raised.value).args[0] == "HY000"
    assert not is_disconnect_error(raised.value)
    assert listened_engine.log_events == []


# `read_all_result_sets` integration (DB-IMPLEMENTATION.md, R2/R3): reader failures take SQLAlchemy's error path, and
# whole operations around the reader are what retries replay.
def assert_cursor_closed_once(caplog: pytest.LogCaptureFixture) -> None:
    """A second close of a pyodbc cursor fails, and SQLAlchemy logs that as "Error closing cursor"."""
    assert [record.getMessage() for record in caplog.records if "closing cursor" in record.getMessage()] == []


@pytest.mark.parametrize("execution_options", [{}, _SKIP_ROWCOUNTS], ids=["default", "opted-in"])
def test_reader_errors_reach_sqlalchemy_error_handling(
        listened_engine: ListenedEngine,
        caplog: pytest.LogCaptureFixture,
        execution_options: dict[str, Any],
) -> None:
    with caplog.at_level(logging.ERROR), listened_engine.engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        result = connection.execute(text(_FAILS_AFTER_A_ROWSET), execution_options=execution_options)
        context = result.context

        with pytest.raises(DBAPIError) as raised:
            read_all_result_sets(result)
        assert result.closed
        assert connection.in_transaction()

    assert_wraps_conversion_error(raised.value)
    assert listened_engine.handled == [HandledError(original_error(raised.value), raised.value, False, context, None)]
    # The batch failed before restoring NOCOUNT; the error-only reset restored it on return to the pool.
    assert _session_state(listened_engine.engine) == (spid, 0)
    assert_cursor_closed_once(caplog)


def test_reader_disconnects_invalidate_without_reconnecting(
        mssql_databases: ManagedE2eDatabases,
        listened_engine: ListenedEngine,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    with listened_engine.engine.connect() as connection:
        spid = connection.execute(text("SELECT @@SPID")).scalar_one()
        result = connection.execute(text(f"{_LARGE_SELECT}; SELECT 2 AS b"))
        kill_spid(spid, mssql_databases.app_engine)
        connects = record_connects(listened_engine.engine, monkeypatch)

        with pytest.raises(DBAPIError) as raised:
            read_all_result_sets(result)
        assert result.closed
        assert connection.invalidated

    assert connects == []
    assert raised.value.__cause__ is raised.value.orig
    assert is_retryable_disconnect(raised.value)
    assert [handled.is_disconnect for handled in listened_engine.handled] == [True]
    assert listened_engine.log_events == ["db.disconnect"]
    with listened_engine.engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


@pytest.fixture
def reader_writes(mssql_databases: ManagedE2eDatabases) -> Generator[None]:
    with mssql_databases.app_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS dbo.reader_writes"))
        connection.execute(text("CREATE TABLE dbo.reader_writes (attempt int NOT NULL PRIMARY KEY)"))
    yield
    with mssql_databases.app_engine.begin() as connection:
        connection.execute(text("DROP TABLE dbo.reader_writes"))


def committed_reader_writes(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return list(connection.execute(text("SELECT attempt FROM dbo.reader_writes ORDER BY attempt")).scalars())


@pytest.mark.usefixtures("reader_writes")
def test_retry_on_transient_conflict_reruns_the_whole_reader_operation_after_a_lost_connection(
        mssql_databases: ManagedE2eDatabases,
        listened_engine: ListenedEngine,
) -> None:
    attempts: list[None] = []

    @retry_on_transient_conflict
    def write_and_read(session: Session) -> list[list[dict[str, Any]]]:
        attempts.append(None)
        spid = session.execute(text("SELECT @@SPID")).scalar_one()
        first_rowset = _LARGE_SELECT if len(attempts) == 1 else "SELECT 1 AS a"
        result = session.execute(
            text(
                f"INSERT INTO dbo.reader_writes (attempt) VALUES (:attempt); {first_rowset}; "
                "SELECT COUNT(*) AS n FROM dbo.reader_writes"
            ),
            {"attempt": len(attempts)},
            execution_options=_SKIP_ROWCOUNTS,
        )
        if len(attempts) == 1:
            kill_spid(spid, mssql_databases.app_engine)
        return read_all_result_sets(result)

    with Session(listened_engine.engine) as session:
        result_sets = write_and_read(session)

    assert len(attempts) == 2
    assert result_sets == [[{"a": 1}], [{"n": 1}]]
    # The killed attempt's write was rolled back by SQL Server; only the complete attempt committed.
    assert committed_reader_writes(listened_engine.engine) == [2]
    assert listened_engine.log_events == ["db.disconnect", "db.retry"]


@pytest.mark.usefixtures("reader_writes")
def test_a_reader_error_after_a_valid_rowset_prevents_the_commit(listened_engine: ListenedEngine) -> None:
    attempts: list[None] = []

    @retry_on_transient_conflict
    def write_and_read(session: Session) -> list[list[dict[str, Any]]]:
        attempts.append(None)
        result = session.execute(
            # Divide by zero is a statement-level error, so SQL Server keeps the transaction and its insert.
            text(
                "INSERT INTO dbo.reader_writes (attempt) VALUES (1); SELECT 1 AS a; "
                "UPDATE dbo.reader_writes SET attempt = attempt WHERE attempt = 1 / 0"
            ),
            execution_options=_SKIP_ROWCOUNTS,
        )
        return read_all_result_sets(result)

    with Session(listened_engine.engine) as session, pytest.raises(DataError) as raised:
        write_and_read(session)

    assert len(attempts) == 1
    assert parse_mssql_error(raised.value).error_type is SqlServerErrorType.DIVIDE_BY_ZERO
    assert committed_reader_writes(listened_engine.engine) == []


def test_retry_on_disconnect_reruns_the_whole_reader_operation(
        mssql_databases: ManagedE2eDatabases,
        listened_engine: ListenedEngine,
) -> None:
    attempts: list[None] = []

    @retry_on_disconnect
    def read(session: Session) -> list[list[dict[str, Any]]]:
        attempts.append(None)
        spid = session.execute(text("SELECT @@SPID")).scalar_one()
        first_rowset = _LARGE_SELECT if len(attempts) == 1 else "SELECT 1 AS a"
        result = session.execute(text(f"{first_rowset}; SELECT 2 AS b"))
        if len(attempts) == 1:
            kill_spid(spid, mssql_databases.app_engine)
        return read_all_result_sets(result)

    with Session(listened_engine.engine) as session:
        assert read(session) == [[{"a": 1}], [{"b": 2}]]

    assert len(attempts) == 2
    assert listened_engine.log_events == ["db.disconnect", "db.retry", "db.retry.succeeded"]


def test_connections_replaced_after_invalidation_or_dispose_start_fresh_sessions(
        mssql_databases: ManagedE2eDatabases,
) -> None:
    """pyodbc driver pooling would hand the discarded session back with its SET options intact."""
    assert pyodbc.pooling is False
    engine = create_engine(mssql_databases.app_engine.url, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET LOCK_TIMEOUT 1234")
            connection.invalidate()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT @@LOCK_TIMEOUT").scalar() == -1
            connection.exec_driver_sql("SET LOCK_TIMEOUT 1234")
        engine.dispose()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT @@LOCK_TIMEOUT").scalar() == -1
    finally:
        engine.dispose()
