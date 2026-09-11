from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from grad_pylib.core.db import SqlServerErrorType, parse_mssql_error
from grad_pylib.testing import SqlServerFixtureConfig, create_e2e_database_bundle


@pytest.fixture(scope="module")
def mssql_error_engine() -> Generator[Engine]:
    fixture_config = SqlServerFixtureConfig(
        migration_runner=lambda _engine: None,
        tables_to_clean=(),
        database_prefix="GradPyLibParserErrors",
    )
    with create_e2e_database_bundle(fixture_config) as databases:
        with databases.app_engine.begin() as connection:
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
        yield databases.app_engine


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
