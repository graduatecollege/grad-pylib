from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from grad_pylib.testing import ManagedE2eDatabases
from grad_pylib.tools.migrate import checksum, run_migrations, split_batches


@pytest.fixture(params=["pyodbc", "pymssql"])
def migration_engine(request: pytest.FixtureRequest, mssql_databases: ManagedE2eDatabases) -> Generator[Engine]:
    """Run migrations through both drivers; `rebuild_models` uses pymssql."""
    app_engine = mssql_databases.app_engine
    engine = app_engine
    if request.param == "pymssql":
        engine = create_engine(app_engine.url.set(drivername="mssql+pymssql", query={}))
    yield engine
    if engine is not app_engine:
        engine.dispose()
    metadata = MetaData()
    metadata.reflect(app_engine)
    metadata.drop_all(app_engine)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    path = tmp_path / "schema"
    path.mkdir()
    return path


def applied_history(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT file_name, checksum FROM schema_migrations ORDER BY file_name"))
        return [tuple(row) for row in rows]


def test_split_batches_supports_go_delimiter() -> None:
    batches = split_batches("create table a(id int);\ngo\ncreate table b(id int);")
    assert batches == ["create table a(id int);", "create table b(id int);"]


def test_run_migrations_uses_schema_dir_parameter(migration_engine: Engine, schema_dir: Path) -> None:
    sql = "CREATE TABLE demo (id int NOT NULL);\nGO\nINSERT INTO demo (id) VALUES (1);"
    (schema_dir / "001-create.sql").write_text(sql, encoding="utf-8")

    run_migrations(migration_engine, schema_dir=schema_dir)

    with migration_engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM demo")).scalars().all() == [1]
    assert applied_history(migration_engine) == [("001-create.sql", checksum(sql))]


def test_run_migrations_skips_applied_files_and_rejects_changed_ones(
        migration_engine: Engine, schema_dir: Path,
) -> None:
    migration = schema_dir / "001-create.sql"
    migration.write_text("CREATE TABLE demo (id int NOT NULL);", encoding="utf-8")
    run_migrations(migration_engine, schema_dir=schema_dir)

    run_migrations(migration_engine, schema_dir=schema_dir)

    migration.write_text("CREATE TABLE demo (id bigint NOT NULL);", encoding="utf-8")
    with pytest.raises(RuntimeError, match="001-create.sql has changed since it was applied"):
        run_migrations(migration_engine, schema_dir=schema_dir)
    assert [name for name, _ in applied_history(migration_engine)] == ["001-create.sql"]


def test_run_migrations_rolls_back_a_failed_file_without_recording_it(
        migration_engine: Engine, schema_dir: Path,
) -> None:
    (schema_dir / "001-create.sql").write_text("CREATE TABLE kept (id int NOT NULL);", encoding="utf-8")
    (schema_dir / "002-fail.sql").write_text(
        "CREATE TABLE rolled_back (id int NOT NULL);\nGO\nINSERT INTO missing_table (id) VALUES (1);",
        encoding="utf-8",
    )

    with pytest.raises(DBAPIError):
        run_migrations(migration_engine, schema_dir=schema_dir)

    table_names = set(inspect(migration_engine).get_table_names())
    assert {"kept", "schema_migrations"} <= table_names
    assert "rolled_back" not in table_names
    assert [name for name, _ in applied_history(migration_engine)] == ["001-create.sql"]


@pytest.mark.parametrize(
    "failing_statement",
    ["SELECT CAST('x' AS int) AS b", "INSERT INTO early (id) VALUES (1 / 0)"],
    ids=["delayed-select", "statement-level-error"],
)
def test_run_migrations_does_not_record_a_file_that_fails_after_a_valid_result(
        migration_engine: Engine, schema_dir: Path, failing_statement: str,
) -> None:
    (schema_dir / "001-fail.sql").write_text(
        f"CREATE TABLE early (id int NOT NULL);\nGO\nINSERT INTO early (id) VALUES (1); SELECT id FROM early; "
        f"{failing_statement}",
        encoding="utf-8",
    )

    with pytest.raises(DBAPIError):
        run_migrations(migration_engine, schema_dir=schema_dir)

    assert "early" not in inspect(migration_engine).get_table_names()
    assert applied_history(migration_engine) == []


def test_run_migrations_completes_writes_after_a_large_discarded_rowset(
        migration_engine: Engine, schema_dir: Path,
) -> None:
    sql = (
        "CREATE TABLE required (id int NOT NULL);\nGO\n"
        "SELECT TOP (100000) a.object_id FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b; "
        "INSERT INTO required (id) VALUES (1);"
    )
    (schema_dir / "001-required.sql").write_text(sql, encoding="utf-8")

    run_migrations(migration_engine, schema_dir=schema_dir)

    with migration_engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM required")).scalars().all() == [1]
    assert applied_history(migration_engine) == [("001-required.sql", checksum(sql))]
