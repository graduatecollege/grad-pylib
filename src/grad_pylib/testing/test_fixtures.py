from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import MagicMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pyodbc
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from grad_pylib.core import db as db_module
from grad_pylib.testing import fixtures as fixtures_module
from grad_pylib.testing.fixtures import (
    CODEBOOK_DATABASE_NAME,
    E2eServerConfig,
    ManagedE2eDatabases,
    SessionDependencyOverride,
    SqlServerFixtureConfig,
    _create_app_engine,
    build_test_app,
    configure_e2e_environment,
    create_codebook_engine,
    create_db_session_fixture,
    provision_sql_server_database_from_file,
    run_e2e_server,
)


class _FakeSettings:
    model_config: dict[str, object] = {"env_file": ".env"}

    def __init__(self) -> None:
        self.env_file = type(self).model_config["env_file"]


@lru_cache(maxsize=1)
def _get_settings() -> _FakeSettings:
    return _FakeSettings()


def _get_app_session() -> Session:
    raise AssertionError("app session should be overridden")


def _get_codebook_session() -> Session:
    raise AssertionError("codebook session should be overridden")


def _fixture_config(*, tables_to_clean: tuple[str, ...] = ()) -> SqlServerFixtureConfig:
    return SqlServerFixtureConfig(
        migration_runner=lambda _engine: None,
        tables_to_clean=tables_to_clean,
    )


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value


class _RecordingMasterConnection:
    def __init__(self, recorded_sql: list[str], *, database_exists: bool) -> None:
        self.recorded_sql = recorded_sql
        self.database_exists = database_exists
        self.invalidated = False

    def __enter__(self) -> _RecordingMasterConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def exec_driver_sql(self, sql: str) -> _ScalarResult:
        self.recorded_sql.append(sql.strip())
        if "sp_getapplock" in sql:
            return _ScalarResult(1)
        if "SELECT DB_ID" in sql:
            return _ScalarResult(1 if self.database_exists else None)
        return _ScalarResult(None)


class _RecordingMasterEngine:
    def __init__(self, connection: _RecordingMasterConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> _RecordingMasterConnection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


class _DisposableEngine:
    def __init__(self, url: object) -> None:
        self.url = url
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _FakeDatabases:
    def __init__(self, app_engine: object, codebook_engine: object) -> None:
        self.app_engine = app_engine
        self._codebook_engine = codebook_engine
        self.requested_databases: list[str] = []

    def engine_for(self, database_name: str) -> object:
        self.requested_databases.append(database_name)
        return self._codebook_engine


def _unconnected_mssql_engine(database: str) -> Engine:
    """Return a SQL Server engine for tests that only inspect its URL; it never connects."""
    return create_engine(URL.create(
        "mssql+pyodbc", host="localhost", database=database, query={"driver": "ODBC Driver 18 for SQL Server"},
    ))


def _make_master_engine(
    recorded_sql: list[str],
    *,
    database_exists: bool,
) -> _RecordingMasterEngine:
    return _RecordingMasterEngine(
        _RecordingMasterConnection(recorded_sql, database_exists=database_exists),
    )


@pytest.mark.parametrize("mode", ["controller", "sequential", "e2e"])
def test_container_startup_configures_memory_before_provisioning(
    mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = MagicMock()
    container.__enter__.return_value = container
    container.get_connection_url.return_value = "mssql+pymssql://localhost/master"
    container_factory = MagicMock(return_value=container)
    monkeypatch.setattr("testcontainers.mssql.SqlServerContainer", container_factory)
    monkeypatch.setattr("testcontainers.community.mssql.SqlServerContainer", container_factory)
    configure_memory = MagicMock()
    monkeypatch.setattr(fixtures_module, "configure_sql_server_memory", configure_memory)
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr(fixtures_module, "_create_app_engine", lambda _url: engine)
    admin_url = fixtures_module._build_pyodbc_url(container.get_connection_url())

    def create_database(url: str, _name: str) -> str:
        configure_memory.assert_called_once_with(url)
        return "app-url"

    monkeypatch.setattr(fixtures_module, "create_database", create_database)
    if mode == "controller":
        state = fixtures_module.SharedSqlServerState()
        try:
            assert fixtures_module.start_controller_container(state, _fixture_config()) == admin_url
            assert fixtures_module.start_controller_container(state, _fixture_config()) == admin_url
            assert state.container is container
            container.start.assert_called_once_with()
        finally:
            fixtures_module.stop_controller_container(state)
        container.stop.assert_called_once_with()
    elif mode == "sequential":
        request = MagicMock(spec=pytest.FixtureRequest)
        request.config = SimpleNamespace()
        generator = fixtures_module.create_mssql_engine(request, _fixture_config())
        try:
            assert next(generator) is engine
        finally:
            generator.close()
        engine.dispose.assert_called_once_with()
        container.__exit__.assert_called_once()
    else:
        with fixtures_module.create_e2e_database_bundle(_fixture_config()) as databases:
            assert databases.app_engine is engine
        engine.dispose.assert_called_once_with()
        container.__exit__.assert_called_once()

    configure_memory.assert_called_once_with(admin_url)
    assert container_factory.call_args.kwargs["mem_limit"] == "2g"
    assert "env" not in container_factory.call_args.kwargs


def test_controller_stops_container_when_memory_configuration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    container = MagicMock()
    container.get_connection_url.return_value = "mssql+pymssql://localhost/master"
    monkeypatch.setattr("testcontainers.mssql.SqlServerContainer", MagicMock(return_value=container))
    error = RuntimeError("configuration failed")
    monkeypatch.setattr(fixtures_module, "configure_sql_server_memory", MagicMock(side_effect=error))
    state = fixtures_module.SharedSqlServerState()

    with pytest.raises(RuntimeError) as raised:
        fixtures_module.start_controller_container(state, _fixture_config())

    assert raised.value is error
    assert state.container is None
    assert state.admin_url is None
    container.stop.assert_called_once_with()


def test_build_test_app_disables_env_file_overrides_sessions_and_skips_lifespan() -> None:
    original_env_file = _FakeSettings.model_config["env_file"]
    startup_calls = 0

    _get_settings.cache_clear()
    _get_settings()

    def create_app() -> FastAPI:
        @asynccontextmanager
        async def _lifespan(_: FastAPI):
            nonlocal startup_calls
            startup_calls += 1
            yield

        app = FastAPI(lifespan=_lifespan)
        app.state.env_file = _get_settings().env_file

        @app.get("/sessions")
        def read_sessions(
            app_session: Annotated[Session, Depends(_get_app_session)],
            codebook_session: Annotated[Session, Depends(_get_codebook_session)],
        ) -> dict[str, str | None]:
            return {
                "app": app_session.bind.url.database,
                "codebook": codebook_session.bind.url.database,
            }

        return app

    try:
        app_engine = _unconnected_mssql_engine("AppTest")
        codebook_engine = _unconnected_mssql_engine("CodebookTest")
        app = build_test_app(
            create_app=create_app,
            settings_type=_FakeSettings,
            get_settings=_get_settings,
            session_overrides=[
                SessionDependencyOverride(_get_app_session, app_engine),
                SessionDependencyOverride(_get_codebook_session, codebook_engine),
            ],
        )

        assert app.state.env_file is None

        with TestClient(app) as client:
            response = client.get("/sessions")

        payload = response.json()
        assert payload == {"app": "AppTest", "codebook": "CodebookTest"}
        assert startup_calls == 0
    finally:
        app_engine.dispose()
        codebook_engine.dispose()
        _FakeSettings.model_config["env_file"] = original_env_file
        _get_settings.cache_clear()


@pytest.mark.parametrize(
    ("database_exists", "expected_batches"),
    [
        pytest.param(
            False,
            ["CREATE DATABASE [Codebook]", "INSERT INTO foo VALUES (1)"],
            id="missing-database",
        ),
        pytest.param(True, [], id="existing-database"),
    ],
)
def test_provision_sql_server_database_from_file_only_runs_missing_database_batches(
    database_exists: bool,
    expected_batches: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text("CREATE DATABASE [Codebook]\nGO\nINSERT INTO foo VALUES (1)\n", encoding="utf-8")
    recorded_sql: list[str] = []
    fake_engine = _make_master_engine(recorded_sql, database_exists=database_exists)
    monkeypatch.setattr("grad_pylib.testing.fixtures.create_engine", lambda *_args, **_kwargs: fake_engine)
    monkeypatch.setattr(fixtures_module, "install_terminated_session_detection", lambda _engine: None)
    monkeypatch.setattr(pyodbc, "pooling", True)

    def record_script_batch(connection: _RecordingMasterConnection, batch: str) -> None:
        assert connection is fake_engine.connection
        recorded_sql.append(batch)

    monkeypatch.setattr("grad_pylib.testing.fixtures.execute_script_batch", record_script_batch)

    provision_sql_server_database_from_file(
        "mssql+pyodbc://localhost/master?driver=ODBC+Driver+18+for+SQL+Server",
        database_name="Codebook",
        sql_file=sql_file,
        lock_name="codebook-lock",
    )

    assert "master.sys.sp_getapplock" in recorded_sql[0]
    assert recorded_sql[1] == "SELECT DB_ID(N'Codebook')"
    assert recorded_sql[2:-1] == expected_batches
    assert recorded_sql[-1] == _RELEASE_CODEBOOK_LOCK
    assert fake_engine.disposed is True
    assert pyodbc.pooling is False


_RELEASE_CODEBOOK_LOCK = "EXEC master.sys.sp_releaseapplock @Resource = 'codebook-lock', @LockOwner = 'Session'"


@pytest.mark.parametrize("invalidated", [False, True], ids=["batch-error", "disconnect"])
def test_provision_sql_server_database_from_file_releases_the_lock_unless_the_session_is_gone(
    invalidated: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text("CREATE DATABASE [Codebook]\n", encoding="utf-8")
    recorded_sql: list[str] = []
    fake_engine = _make_master_engine(recorded_sql, database_exists=False)
    monkeypatch.setattr("grad_pylib.testing.fixtures.create_engine", lambda *_args, **_kwargs: fake_engine)
    monkeypatch.setattr(fixtures_module, "install_terminated_session_detection", lambda _engine: None)
    batch_error = RuntimeError("batch failed")

    def fail_script_batch(connection: _RecordingMasterConnection, _batch: str) -> None:
        connection.invalidated = invalidated
        raise batch_error

    monkeypatch.setattr("grad_pylib.testing.fixtures.execute_script_batch", fail_script_batch)

    with pytest.raises(RuntimeError) as raised:
        provision_sql_server_database_from_file(
            "mssql+pyodbc://localhost/master?driver=ODBC+Driver+18+for+SQL+Server",
            database_name="Codebook",
            sql_file=sql_file,
            lock_name="codebook-lock",
        )

    assert raised.value is batch_error
    assert (recorded_sql[-1] == _RELEASE_CODEBOOK_LOCK) is not invalidated
    assert fake_engine.disposed is True


@pytest.mark.parametrize(
    "primary_error",
    [None, ValueError("batch failed"), DBAPIError("batch", None, pyodbc.Error("HY000", "batch failed")),
     KeyboardInterrupt()],
    ids=["cleanup-only", "non-driver-error", "driver-error", "interrupted"],
)
def test_provision_sql_server_database_from_file_handles_lock_release_failures(
    primary_error: BaseException | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text("SELECT 1", encoding="utf-8")
    recorded_sql: list[str] = []
    fake_engine = _make_master_engine(recorded_sql, database_exists=False)
    monkeypatch.setattr(fixtures_module, "create_engine", lambda *_args, **_kwargs: fake_engine)
    monkeypatch.setattr(fixtures_module, "install_terminated_session_detection", lambda _engine: None)
    release_error = DBAPIError(_RELEASE_CODEBOOK_LOCK, None, pyodbc.Error("HY000", "release failed"))
    execute = fake_engine.connection.exec_driver_sql

    def fail_release(sql: str) -> _ScalarResult:
        result = execute(sql)
        if sql == _RELEASE_CODEBOOK_LOCK:
            raise release_error
        return result

    def execute_batch(_connection: _RecordingMasterConnection, _batch: str) -> None:
        if primary_error is not None:
            raise primary_error

    monkeypatch.setattr(fake_engine.connection, "exec_driver_sql", fail_release)
    monkeypatch.setattr(fixtures_module, "execute_script_batch", execute_batch)
    expected = release_error if primary_error is None else primary_error

    with capture_logs() as logs, pytest.raises(type(expected)) as raised:
        provision_sql_server_database_from_file(
            "mssql+pyodbc://localhost/master?driver=ODBC+Driver+18+for+SQL+Server",
            database_name="Codebook",
            sql_file=sql_file,
            lock_name="codebook-lock",
        )

    assert raised.value is expected
    assert recorded_sql[-1] == _RELEASE_CODEBOOK_LOCK
    assert fake_engine.disposed
    assert logs == ([] if primary_error is None else [{
        "event": "db.provision.lock_release_failed",
        "exc_info": release_error,
        "log_level": "warning",
    }])


@pytest.mark.parametrize("prefix", ["", "SELECT 1 AS a; "], ids=["execute", "after-rowset"])
def test_provision_sql_server_database_from_file_preserves_terminated_session_errors(
    mssql_admin_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str,
) -> None:
    batch = f"{prefix}RAISERROR('provisioning-fatal', 20, 1) WITH LOG;"
    sql_file = tmp_path / "fatal.sql"
    sql_file.write_text(batch, encoding="utf-8")
    errors: list[DBAPIError] = []
    execute = fixtures_module.execute_script_batch

    def capture_batch_error(connection: Connection, statement: str) -> None:
        try:
            execute(connection, statement)
        except DBAPIError as error:
            errors.append(error)
            raise

    monkeypatch.setattr(fixtures_module, "execute_script_batch", capture_batch_error)
    with pytest.raises(DBAPIError) as raised:
        provision_sql_server_database_from_file(
            mssql_admin_url,
            database_name="GradPyLib_terminated_provisioning",
            sql_file=sql_file,
            lock_name="GradPyLib_terminated_provisioning",
        )

    assert errors == [raised.value]
    assert isinstance(raised.value.orig, pyodbc.Error)
    assert raised.value.orig.args[0] == "HY000"
    assert raised.value.__cause__ is raised.value.orig
    assert raised.value.statement == batch
    assert raised.value.connection_invalidated


_LARGE_ROWSET = "SELECT TOP (100000) a.object_id FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b"


@pytest.fixture(params=["pyodbc", "pymssql"])
def provisioning_admin_url(request: pytest.FixtureRequest, mssql_admin_url: str) -> str:
    if request.param == "pyodbc":
        return mssql_admin_url
    return make_url(mssql_admin_url).set(drivername="mssql+pymssql", query={}).render_as_string(hide_password=False)


@pytest.fixture
def provisioned_database(provisioning_admin_url: str, mssql_admin_url: str) -> Generator[str]:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    database_name = f"GradPyLib_provision_{worker_id}_{make_url(provisioning_admin_url).get_driver_name()}"
    yield database_name
    admin_engine = create_engine(mssql_admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(
                f"IF DB_ID(N'{database_name}') IS NOT NULL "
                f"ALTER DATABASE [{database_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
            )
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS [{database_name}]")
    finally:
        admin_engine.dispose()


def _seeded_ids(admin_url: str, database_name: str) -> list[int]:
    engine = create_engine(admin_url)
    try:
        with engine.connect() as connection:
            return list(connection.exec_driver_sql(
                f"SELECT id FROM [{database_name}].dbo.seeded ORDER BY id"
            ).scalars())
    finally:
        engine.dispose()


def test_provision_sql_server_database_from_file_drains_every_batch(
    provisioning_admin_url: str,
    provisioned_database: str,
    mssql_admin_url: str,
    tmp_path: Path,
) -> None:
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text(
        f"CREATE DATABASE [{provisioned_database}]\nGO\n"
        f"USE [{provisioned_database}]; CREATE TABLE dbo.seeded (id int NOT NULL);\nGO\n"
        f"INSERT INTO dbo.seeded VALUES (1); {_LARGE_ROWSET}; INSERT INTO dbo.seeded VALUES (2);\n",
        encoding="utf-8",
    )

    for _ in range(2):
        provision_sql_server_database_from_file(
            provisioning_admin_url, database_name=provisioned_database, sql_file=sql_file, lock_name=provisioned_database,
        )

    # The second call found the database and ran nothing; `USE` carried across batches on one connection.
    assert _seeded_ids(mssql_admin_url, provisioned_database) == [1, 2]


def test_provision_sql_server_database_from_file_reports_failures_after_a_valid_result(
    provisioning_admin_url: str,
    provisioned_database: str,
    mssql_admin_url: str,
    tmp_path: Path,
) -> None:
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text(
        f"CREATE DATABASE [{provisioned_database}]\nGO\n"
        f"USE [{provisioned_database}]; CREATE TABLE dbo.seeded (id int NOT NULL); "
        "SELECT 1 AS a; INSERT INTO dbo.seeded VALUES (1 / 0);\nGO\n"
        "INSERT INTO dbo.seeded VALUES (2);\n",
        encoding="utf-8",
    )

    with pytest.raises(DBAPIError):
        provision_sql_server_database_from_file(
            provisioning_admin_url, database_name=provisioned_database, sql_file=sql_file, lock_name=provisioned_database,
        )

    # AUTOCOMMIT keeps work applied before the failure; later batches never ran.
    assert _seeded_ids(mssql_admin_url, provisioned_database) == []


@pytest.mark.parametrize(
    "source_url",
    [
        pytest.param(make_url("mssql+pyodbc://localhost/App?driver=ODBC+Driver+18+for+SQL+Server"), id="standard-url"),
        pytest.param(
            make_url(
                "mssql+pyodbc:///?odbc_connect="
                "Driver%3D%7BODBC+Driver+18+for+SQL+Server%7D%3BServer%3Dlocalhost%3BDatabase%3DApp"
            ),
            id="odbc-connect",
        ),
    ],
)
def test_create_codebook_engine_targets_codebook_database(
    source_url: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_create_engine(url: object, *, future: bool, pool_pre_ping: bool) -> _DisposableEngine:
        recorded["url"] = url
        recorded["future"] = future
        recorded["pool_pre_ping"] = pool_pre_ping
        engine = _DisposableEngine(url)
        recorded["engine"] = engine
        return engine

    def fake_install_connection_reset(engine: _DisposableEngine) -> None:
        recorded["reset_engine"] = engine

    def fake_install_terminated_session_detection(engine: _DisposableEngine) -> None:
        recorded["detection_engine"] = engine

    def fake_install_mssql_batch_support(engine: _DisposableEngine) -> None:
        recorded["batch_engine"] = engine

    monkeypatch.setattr("grad_pylib.testing.fixtures.create_engine", fake_create_engine)
    monkeypatch.setattr("grad_pylib.testing.fixtures.install_connection_reset", fake_install_connection_reset)
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.install_terminated_session_detection",
        fake_install_terminated_session_detection,
    )
    monkeypatch.setattr("grad_pylib.testing.fixtures.install_mssql_batch_support", fake_install_mssql_batch_support)
    generator = create_codebook_engine(SimpleNamespace(url=source_url))

    assert next(generator) is recorded["engine"]

    rewritten_url = recorded["url"]
    if "odbc_connect" in source_url.query:
        assert make_url(str(rewritten_url)).query["odbc_connect"].endswith("Database=Codebook")
    else:
        assert rewritten_url.database == CODEBOOK_DATABASE_NAME

    assert recorded["future"] is True
    assert recorded["pool_pre_ping"] is True
    assert recorded["reset_engine"] is recorded["engine"]
    assert recorded["detection_engine"] is recorded["engine"]
    assert recorded["batch_engine"] is recorded["engine"]

    with pytest.raises(StopIteration):
        next(generator)

    assert recorded["engine"].disposed is True


def test_app_engines_install_the_runtime_batch_support() -> None:
    managed = ManagedE2eDatabases(
        admin_url="unused",
        app_engine=_create_app_engine("mssql+pyodbc://localhost/App?driver=ODBC+Driver+18+for+SQL+Server"),
    )
    try:
        for engine in (managed.app_engine, managed.engine_for(CODEBOOK_DATABASE_NAME)):
            assert event.contains(engine, "before_execute", db_module._validate_batch_execution)
            assert event.contains(engine, "before_cursor_execute", db_module._validate_driver_sql_batch)
            assert event.contains(engine, "after_cursor_execute", db_module._skip_leading_rowcounts)
    finally:
        managed.dispose()


def test_app_engines_run_opted_in_batches(mssql_databases: ManagedE2eDatabases) -> None:
    with mssql_databases.app_engine.connect() as connection:
        result = connection.execute(
            text("DECLARE @t TABLE (id int); INSERT INTO @t VALUES (1); SELECT id FROM @t"),
            execution_options={"mssql_skip_rowcounts": True},
        )

        assert result.scalars().all() == [1]


def test_create_db_session_fixture_rolls_back_changes_cleans_tables_and_runs_hooks(
    mssql_databases: ManagedE2eDatabases,
) -> None:
    engine = mssql_databases.app_engine
    events: list[str] = []
    row_counts_after_cleanup: list[int] = []

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE widgets (id int)")
        conn.exec_driver_sql("INSERT INTO widgets (id) VALUES (1)")

    def after_test() -> None:
        events.append("after")
        with engine.connect() as conn:
            row_counts_after_cleanup.append(conn.exec_driver_sql("SELECT COUNT(*) FROM widgets").scalar_one())

    generator = create_db_session_fixture(
        engine,
        _fixture_config(tables_to_clean=("widgets",)),
        before_test=lambda: events.append("before"),
        after_test=after_test,
    )

    session = next(generator)
    session.execute(text("INSERT INTO widgets (id) VALUES (2)"))

    with pytest.raises(StopIteration):
        next(generator)

    with engine.connect() as conn:
        remaining_rows = conn.exec_driver_sql("SELECT COUNT(*) FROM widgets").scalar()

    assert events == ["before", "after"]
    assert row_counts_after_cleanup == [0]
    assert remaining_rows == 0


def test_configure_e2e_environment_sets_required_values_and_preserves_allowed_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2E_HOST", "127.0.0.1")
    monkeypatch.setenv("E2E_PORT", "9001")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://example.com"]')

    host, port = configure_e2e_environment(
        E2eServerConfig(
            dev_api_key="dev-key",
            seed_dataset=lambda _session: None,
            build_app=lambda _app_engine, _codebook_engine: FastAPI(),
        )
    )

    assert (host, port) == ("127.0.0.1", 9001)
    assert os.environ["ENVIRONMENT"] == "test"
    assert os.environ["ENABLE_DEV_API_KEY"] == "true"
    assert os.environ["DEV_API_KEY"] == "dev-key"
    assert os.environ["AZURE_AD_CLIENT_ID"] == "e2e-client-id"
    assert os.environ["AZURE_AD_TENANT_ID"] == "e2e-tenant-id"
    assert os.environ["ALLOWED_ORIGINS"] == '["https://example.com"]'


def test_run_e2e_server_orchestrates_migration_seed_and_server_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_engine = _unconnected_mssql_engine("AppE2e")
    codebook_engine = _unconnected_mssql_engine("Codebook")
    databases = _FakeDatabases(app_engine=app_engine, codebook_engine=codebook_engine)
    recorded_steps: list[tuple[str, object]] = []
    app = FastAPI()

    @contextmanager
    def fake_bundle(*_args: object, **_kwargs: object):
        try:
            yield databases
        finally:
            app_engine.dispose()
            codebook_engine.dispose()

    def migration_runner(engine: object) -> None:
        recorded_steps.append(("migrate", engine))

    def provision_codebook_database(engine: object) -> None:
        recorded_steps.append(("provision_codebook", engine))

    def seed_dataset(session: Session) -> None:
        recorded_steps.append(("seed", session.bind.url.database))

    def build_app(app_db_engine: object, maybe_codebook_engine: object) -> FastAPI:
        recorded_steps.append(("build_app", (app_db_engine, maybe_codebook_engine)))
        return app

    def fake_run_server(server_app: object, *, host: str, port: int, access_log: bool) -> None:
        recorded_steps.append(("serve", (server_app, host, port, access_log)))

    monkeypatch.setattr("grad_pylib.testing.fixtures.configure_e2e_environment", lambda _config: ("127.0.0.1", 9001))
    monkeypatch.setattr("grad_pylib.testing.fixtures.create_e2e_database_bundle", fake_bundle)
    monkeypatch.setattr("grad_pylib.testing.fixtures.run_managed_uvicorn_server", fake_run_server)

    run_e2e_server(
        SimpleNamespace(migration_runner=migration_runner),
        E2eServerConfig(
            dev_api_key="dev-key",
            seed_dataset=seed_dataset,
            build_app=build_app,
            codebook_database_name="Codebook",
            provision_codebook_database=provision_codebook_database,
        ),
    )

    assert databases.requested_databases == ["Codebook"]
    assert recorded_steps == [
        ("migrate", app_engine),
        ("provision_codebook", app_engine),
        ("seed", "AppE2e"),
        ("build_app", (app_engine, codebook_engine)),
        ("serve", (app, "127.0.0.1", 9001, True)),
    ]
