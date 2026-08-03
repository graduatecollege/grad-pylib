from functools import lru_cache
import signal
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Generator, Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest
import uvicorn
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from uvicorn.server import HANDLED_SIGNALS

from grad_pylib.testing.fixtures import (
    CODEBOOK_DATABASE_NAME,
    CleanupFriendlyUvicornServer,
    E2eServerConfig,
    ManagedE2eDatabases,
    SessionDependencyOverride,
    SqlServerBootstrapConfig,
    SqlServerFixtureConfig,
    SqlServerTestBootstrap,
    build_test_app,
    create_codebook_engine,
    create_db_session_fixture,
    ensure_sql_server_database,
    provision_sql_server_database_from_file,
    run_e2e_server,
    split_go_batches,
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


def test_build_test_app_disables_env_file_overrides_sessions_and_skips_lifespan(tmp_path: Path) -> None:
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


def test_managed_e2e_databases_reuses_named_engines(tmp_path: Path) -> None:
    app_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    databases = ManagedE2eDatabases(admin_url="sqlite+pysqlite:///master.db", app_engine=app_engine)

    try:
        codebook_engine = databases.engine_for(str(tmp_path / "codebook.db"))
        same_codebook_engine = databases.engine_for(str(tmp_path / "codebook.db"))
        audit_engine = databases.engine_for(str(tmp_path / "audit.db"))

        assert codebook_engine is same_codebook_engine
        assert codebook_engine is not audit_engine
        assert str(codebook_engine.url).endswith("codebook.db")
        assert str(audit_engine.url).endswith("audit.db")
    finally:
        databases.dispose()


def test_split_go_batches_splits_sql_server_batches() -> None:
    batches = split_go_batches("SELECT 1\nGO\n\nSELECT 2\n go \nSELECT 3")

    assert batches == ["SELECT 1", "SELECT 2", "SELECT 3"]


def test_provision_sql_server_database_from_file_runs_batches_once(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text("CREATE DATABASE [Codebook]\nGO\nINSERT INTO foo VALUES (1)\n", encoding="utf-8")
    recorded: list[str] = []

    class _ScalarResult:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar(self) -> object:
            return self.value

    class _FakeConnection:
        def __enter__(self) -> _FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def exec_driver_sql(self, sql: str) -> _ScalarResult:
            recorded.append(sql.strip())
            if "sp_getapplock" in sql:
                return _ScalarResult(1)
            if "SELECT DB_ID" in sql:
                return _ScalarResult(None)
            return _ScalarResult(None)

    class _FakeEngine:
        def __init__(self) -> None:
            self.connection = _FakeConnection()
            self.disposed = False

        def connect(self) -> _FakeConnection:
            return self.connection

        def dispose(self) -> None:
            self.disposed = True

    fake_engine = _FakeEngine()
    monkeypatch.setattr("grad_pylib.testing.fixtures.create_engine", lambda *_args, **_kwargs: fake_engine)

    provision_sql_server_database_from_file(
        "mssql+pyodbc://localhost/master?driver=ODBC+Driver+18+for+SQL+Server",
        database_name="Codebook",
        sql_file=sql_file,
        lock_name="codebook-lock",
    )

    assert "sp_getapplock" in recorded[0]
    assert recorded[1] == "SELECT DB_ID(N'Codebook')"
    assert recorded[2:] == ["CREATE DATABASE [Codebook]", "INSERT INTO foo VALUES (1)"]
    assert fake_engine.disposed is True


def test_ensure_sql_server_database_targets_master(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded: dict[str, Any] = {}

    def fake_provision(
            admin_url: str,
            *,
            database_name: str,
            sql_file: Path,
            lock_name: str,
    ) -> None:
        recorded["call"] = {
            "admin_url": admin_url,
            "database_name": database_name,
            "sql_file": sql_file,
            "lock_name": lock_name,
        }

    monkeypatch.setattr("grad_pylib.testing.fixtures.provision_sql_server_database_from_file", fake_provision)
    engine = SimpleNamespace(
        url=make_url(
            "mssql+pyodbc://sa:Password%21@localhost/app?driver=ODBC+Driver+18+for+SQL+Server"
        )
    )
    sql_file = tmp_path / "codebook.sql"

    ensure_sql_server_database(
        engine,
        database_name="Codebook",
        sql_file=sql_file,
        lock_name="codebook-lock",
    )

    assert recorded["call"] == {
        "admin_url": (
            "mssql+pyodbc://sa:Password%21@localhost/master?driver=ODBC+Driver+18+for+SQL+Server"
        ),
        "database_name": "Codebook",
        "sql_file": sql_file,
        "lock_name": "codebook-lock",
    }


def test_sql_server_bootstrap_config_requires_codebook_sql_path() -> None:
    fixture_config = SqlServerFixtureConfig(migration_runner=lambda _engine: None, tables_to_clean=())

    with pytest.raises(ValueError, match="codebook_sql_path is required"):
        SqlServerBootstrapConfig(fixture_config=fixture_config, include_codebook_engine=True)


def test_sql_server_test_bootstrap_provisions_codebook_for_controller(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_config = SqlServerFixtureConfig(migration_runner=lambda _engine: None, tables_to_clean=())
    codebook_sql_path = tmp_path / "codebook.sql"
    bootstrap = SqlServerTestBootstrap(
        SqlServerBootstrapConfig(
            fixture_config=fixture_config,
            codebook_sql_path=codebook_sql_path,
        )
    )
    recorded: dict[str, object] = {}

    def fake_start_controller_container(
            state: object,
            received_fixture_config: SqlServerFixtureConfig,
    ) -> str:
        recorded["start"] = (state, received_fixture_config)
        return "mssql+pyodbc://localhost/master"

    def fake_provision_codebook_database(admin_url: str, *, sql_file: Path, lock_name: str) -> None:
        recorded["provision"] = (admin_url, sql_file, lock_name)

    monkeypatch.setattr("grad_pylib.testing.fixtures.is_xdist_controller", lambda _config: True)
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.start_controller_container",
        fake_start_controller_container,
    )
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.provision_codebook_database",
        fake_provision_codebook_database,
    )

    bootstrap.pytest_configure(SimpleNamespace())

    assert recorded["start"] == (bootstrap.state, fixture_config)
    assert recorded["provision"] == (
        "mssql+pyodbc://localhost/master",
        codebook_sql_path,
        "grad-pylib-test-codebook",
    )


def test_sql_server_test_bootstrap_configures_workers_and_stops_controller(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_config = SqlServerFixtureConfig(migration_runner=lambda _engine: None, tables_to_clean=())
    bootstrap = SqlServerTestBootstrap(SqlServerBootstrapConfig(fixture_config=fixture_config))
    recorded: dict[str, object] = {}
    node = SimpleNamespace()

    def fake_configure_worker_node(
            received_node: object,
            *,
            state: object,
            fixture_config: SqlServerFixtureConfig,
    ) -> None:
        recorded["configure"] = (received_node, state, fixture_config)

    def fake_stop_controller_container(state: object) -> None:
        recorded["stopped"] = [state]

    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.configure_worker_node",
        fake_configure_worker_node,
    )
    monkeypatch.setattr("grad_pylib.testing.fixtures.is_xdist_controller", lambda _config: True)
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.stop_controller_container",
        fake_stop_controller_container,
    )

    bootstrap.pytest_configure_node(node)
    bootstrap.pytest_unconfigure(SimpleNamespace())

    assert recorded["configure"] == (node, bootstrap.state, fixture_config)
    assert recorded["stopped"] == [bootstrap.state]


def test_sql_server_test_bootstrap_mssql_engine_ensures_codebook_outside_worker(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_config = SqlServerFixtureConfig(migration_runner=lambda _engine: None, tables_to_clean=())
    codebook_sql_path = tmp_path / "codebook.sql"
    bootstrap = SqlServerTestBootstrap(
        SqlServerBootstrapConfig(
            fixture_config=fixture_config,
            codebook_sql_path=codebook_sql_path,
        )
    )
    recorded: dict[str, object] = {}
    engine = create_engine("sqlite+pysqlite:///:memory:")

    def fake_create_mssql_engine(
            request: pytest.FixtureRequest,
            received_fixture_config: SqlServerFixtureConfig,
    ) -> Generator[object]:
        recorded["create"] = (request, received_fixture_config)
        try:
            yield engine
        finally:
            recorded["closed"] = True

    def fake_ensure_codebook_database(received_engine: object, *, sql_file: Path, lock_name: str) -> None:
        recorded["ensure"] = (received_engine, sql_file, lock_name)

    monkeypatch.setattr("grad_pylib.testing.fixtures.create_mssql_engine", fake_create_mssql_engine)
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.ensure_codebook_database",
        fake_ensure_codebook_database,
    )

    request = SimpleNamespace(config=SimpleNamespace())
    generator = bootstrap.mssql_engine(request)

    assert next(generator) is engine
    with pytest.raises(StopIteration):
        next(generator)

    assert recorded["create"] == (request, fixture_config)
    assert recorded["ensure"] == (engine, codebook_sql_path, "grad-pylib-test-codebook")
    assert recorded["closed"] is True
    engine.dispose()


def test_sql_server_test_bootstrap_codebook_engine_requires_opt_in(tmp_path: Path) -> None:
    fixture_config = SqlServerFixtureConfig(migration_runner=lambda _engine: None, tables_to_clean=())
    bootstrap = SqlServerTestBootstrap(SqlServerBootstrapConfig(fixture_config=fixture_config))
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    generator = bootstrap.codebook_engine(engine)

    try:
        with pytest.raises(RuntimeError, match="include_codebook_engine must be true"):
            next(generator)
    finally:
        engine.dispose()


def test_create_codebook_engine_targets_codebook_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    class _FakeEngine:
        def __init__(self, url: object) -> None:
            self.url = url
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    def fake_create_engine(url: object, *, future: bool, pool_pre_ping: bool) -> _FakeEngine:
        recorded["call"] = {"url": url, "future": future, "pool_pre_ping": pool_pre_ping}
        engine = _FakeEngine(url)
        recorded["engine"] = engine
        return engine

    monkeypatch.setattr("grad_pylib.testing.fixtures.create_engine", fake_create_engine)
    mssql_engine = SimpleNamespace(url=make_url("sqlite:///app.db"))
    generator = create_codebook_engine(mssql_engine)

    assert next(generator) is recorded["engine"]
    assert recorded["call"] == {
        "url": mssql_engine.url.set(database=CODEBOOK_DATABASE_NAME),
        "future": True,
        "pool_pre_ping": True,
    }

    with pytest.raises(StopIteration):
        next(generator)

    assert recorded["engine"].disposed is True


def test_create_db_session_fixture_runs_optional_hooks(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    fixture_config = SqlServerFixtureConfig(
        migration_runner=lambda _engine: None,
        tables_to_clean=("widgets",),
    )
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
        fixture_config,
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


def test_run_e2e_server_configures_environment_and_runs_with_optional_codebook(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    codebook_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'codebook.db'}")
    databases = ManagedE2eDatabases(admin_url="sqlite+pysqlite:///master.db", app_engine=app_engine)
    databases._named_engines["Codebook"] = codebook_engine
    recorded: dict[str, object] = {}
    app = FastAPI()

    @contextmanager
    def fake_bundle(*_args: object, **_kwargs: object) -> Iterator[ManagedE2eDatabases]:
        try:
            yield databases
        finally:
            databases.dispose()

    def migration_runner(engine: object) -> None:
        recorded["migrated"] = engine

    def provision_codebook_database(engine: object) -> None:
        recorded["provisioned"] = engine

    def seed_dataset(session: Session) -> None:
        recorded["seeded_database"] = session.bind.url.database

    def build_app(app_db_engine: object, maybe_codebook_engine: object) -> FastAPI:
        recorded["build_app"] = (app_db_engine, maybe_codebook_engine)
        return app

    def fake_run_managed_uvicorn_server(server_app: object, *, host: str, port: int, access_log: bool) -> None:
        recorded["uvicorn"] = (server_app, host, port, access_log)

    monkeypatch.setattr("grad_pylib.testing.fixtures.create_e2e_database_bundle", fake_bundle)
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.run_managed_uvicorn_server",
        fake_run_managed_uvicorn_server,
    )
    monkeypatch.setenv("E2E_HOST", "127.0.0.1")
    monkeypatch.setenv("E2E_PORT", "9001")
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    fixture_config = SimpleNamespace(migration_runner=migration_runner)
    run_e2e_server(
        fixture_config,
        E2eServerConfig(
            dev_api_key="dev-key",
            seed_dataset=seed_dataset,
            build_app=build_app,
            codebook_database_name="Codebook",
            provision_codebook_database=provision_codebook_database,
        ),
    )

    assert recorded["migrated"] is app_engine
    assert recorded["provisioned"] is app_engine
    assert recorded["seeded_database"].endswith("app.db")
    assert recorded["build_app"] == (app_engine, codebook_engine)
    assert recorded["uvicorn"] == (app, "127.0.0.1", 9001, True)
    assert _get_env("ENVIRONMENT") == "test"
    assert _get_env("ENABLE_DEV_API_KEY") == "true"
    assert _get_env("DEV_API_KEY") == "dev-key"
    assert _get_env("AZURE_AD_CLIENT_ID") == "e2e-client-id"
    assert _get_env("AZURE_AD_TENANT_ID") == "e2e-tenant-id"
    assert _get_env("ALLOWED_ORIGINS") == (
        '["http://localhost:5173", "http://localhost:3000", '
        '"http://127.0.0.1:5173", "http://127.0.0.1:3000"]'
    )


def test_run_e2e_server_skips_codebook_when_not_configured(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    databases = ManagedE2eDatabases(admin_url="sqlite+pysqlite:///master.db", app_engine=app_engine)
    recorded: dict[str, object] = {}

    @contextmanager
    def fake_bundle(*_args: object, **_kwargs: object) -> Iterator[ManagedE2eDatabases]:
        try:
            yield databases
        finally:
            databases.dispose()

    def migration_runner(_engine: object) -> None:
        recorded["migrated"] = True

    def seed_dataset(_session: Session) -> None:
        recorded["seeded"] = True

    def build_app(_app_engine: object, maybe_codebook_engine: object) -> FastAPI:
        recorded["codebook_engine"] = maybe_codebook_engine
        return FastAPI()

    monkeypatch.setattr("grad_pylib.testing.fixtures.create_e2e_database_bundle", fake_bundle)
    monkeypatch.setattr(
        "grad_pylib.testing.fixtures.run_managed_uvicorn_server",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    fixture_config = SimpleNamespace(migration_runner=migration_runner)
    run_e2e_server(
        fixture_config,
        E2eServerConfig(
            dev_api_key="dev-key",
            seed_dataset=seed_dataset,
            build_app=build_app,
        ),
    )

    assert recorded["migrated"] is True
    assert recorded["seeded"] is True
    assert recorded["codebook_engine"] is None


def test_cleanup_friendly_uvicorn_server_does_not_reraise_captured_signals(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_signals: list[tuple[signal.Signals, object]] = []
    restored_handlers = {sig: object() for sig in HANDLED_SIGNALS}
    raised_signals: list[int] = []

    server = CleanupFriendlyUvicornServer(uvicorn.Config(FastAPI()))

    def fake_signal(sig: signal.Signals, handler: object) -> object:
        recorded_signals.append((sig, handler))
        return restored_handlers.setdefault(sig, object())

    monkeypatch.setattr("grad_pylib.testing.fixtures.signal.signal", fake_signal)
    monkeypatch.setattr("grad_pylib.testing.fixtures.signal.raise_signal", raised_signals.append)

    with server.capture_signals():
        server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit is True
    assert raised_signals == []
    assert len(recorded_signals) == len(HANDLED_SIGNALS) * 2


def _get_env(name: str) -> str:
    import os

    return os.environ[name]
