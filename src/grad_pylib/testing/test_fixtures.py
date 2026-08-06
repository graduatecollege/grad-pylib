from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from grad_pylib.testing.fixtures import (
    CODEBOOK_DATABASE_NAME,
    E2eServerConfig,
    SessionDependencyOverride,
    SqlServerFixtureConfig,
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


def _make_master_engine(
    recorded_sql: list[str],
    *,
    database_exists: bool,
) -> _RecordingMasterEngine:
    return _RecordingMasterEngine(
        _RecordingMasterConnection(recorded_sql, database_exists=database_exists),
    )


def test_build_test_app_disables_env_file_overrides_sessions_and_skips_lifespan(
    tmp_path: Path,
) -> None:
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
        app_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app-test.db'}")
        codebook_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'codebook-test.db'}")
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
        assert payload["app"].endswith("app-test.db")
        assert payload["codebook"].endswith("codebook-test.db")
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

    provision_sql_server_database_from_file(
        "mssql+pyodbc://localhost/master?driver=ODBC+Driver+18+for+SQL+Server",
        database_name="Codebook",
        sql_file=sql_file,
        lock_name="codebook-lock",
    )

    assert "sp_getapplock" in recorded_sql[0]
    assert recorded_sql[1] == "SELECT DB_ID(N'Codebook')"
    assert recorded_sql[2:] == expected_batches
    assert fake_engine.disposed is True


@pytest.mark.parametrize(
    "source_url",
    [
        pytest.param(make_url("sqlite:///app.db"), id="standard-url"),
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

    monkeypatch.setattr("grad_pylib.testing.fixtures.create_engine", fake_create_engine)
    generator = create_codebook_engine(SimpleNamespace(url=source_url))

    assert next(generator) is recorded["engine"]

    rewritten_url = recorded["url"]
    if "odbc_connect" in source_url.query:
        assert make_url(str(rewritten_url)).query["odbc_connect"].endswith("Database=Codebook")
    else:
        assert rewritten_url.database == CODEBOOK_DATABASE_NAME

    assert recorded["future"] is True
    assert recorded["pool_pre_ping"] is True

    with pytest.raises(StopIteration):
        next(generator)

    assert recorded["engine"].disposed is True


def test_create_db_session_fixture_rolls_back_changes_cleans_tables_and_runs_hooks(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    events: list[str] = []
    row_counts_after_cleanup: list[int] = []

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE widgets (id INTEGER)")
        conn.exec_driver_sql("INSERT INTO widgets (id) VALUES (1)")

    def after_test() -> None:
        events.append("after")
        with engine.connect() as conn:
            row_counts_after_cleanup.append(conn.exec_driver_sql("SELECT COUNT(*) FROM widgets").scalar())

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
    engine.dispose()


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
    tmp_path: Path,
) -> None:
    app_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    codebook_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'codebook.db'}")
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
        ("seed", str(tmp_path / "app.db")),
        ("build_app", (app_engine, codebook_engine)),
        ("serve", (app, "127.0.0.1", 9001, True)),
    ]
