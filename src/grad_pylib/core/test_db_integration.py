from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from grad_pylib.core.db import (
    SqlServerErrorType,
    describe_disconnect,
    is_disconnect_error,
    is_retryable_disconnect,
    parse_mssql_error,
    retry_on_transient_conflict,
)
from grad_pylib.testing import ManagedE2eDatabases, SqlServerFixtureConfig, create_e2e_database_bundle


@pytest.fixture(scope="module")
def mssql_databases() -> Generator[ManagedE2eDatabases]:
    fixture_config = SqlServerFixtureConfig(
        migration_runner=lambda _engine: None,
        tables_to_clean=(),
        database_prefix="GradPyLibDbIntegration",
    )
    with create_e2e_database_bundle(fixture_config) as databases:
        yield databases


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
    spid = session.execute(text("SELECT @@SPID")).scalar_one()
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
        session.commit()

    with lost_connection_sessions() as session:
        insert_row(session)
        rows = session.execute(text("SELECT id FROM dbo.lost_connection_writes")).scalars().all()

    assert len(attempts) == 2
    assert rows == [1]
