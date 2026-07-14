import os
import re
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from fastapi import FastAPI
import uvicorn
from sqlalchemy import create_engine, text, Engine
from sqlalchemy.orm import sessionmaker, Session
import pytest
from _pytest.config import Config
from xdist.workermanage import WorkerController

from grad_pylib.tools.rebuild_models import DEFAULT_SQL_SERVER_IMAGE

CODEBOOK_DATABASE_NAME = "Codebook"


@dataclass(frozen=True, slots=True)
class SqlServerFixtureConfig:
    migration_runner: Callable[[Engine], None]
    tables_to_clean: tuple[str, ...]
    image: str = DEFAULT_SQL_SERVER_IMAGE
    password: str = "Test@12345!"
    database_prefix: str = "AppTest"


class SettingsFactory(Protocol):
    def __call__(self) -> object: ...

    def cache_clear(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionDependencyOverride:
    dependency: Callable[..., object]
    engine: Engine


@dataclass(frozen=True, slots=True)
class E2eServerConfig:
    dev_api_key: str
    seed_dataset: Callable[[Session], None]
    build_app: Callable[[Engine, Engine | None], FastAPI]
    codebook_database_name: str | None = None
    provision_codebook_database: Callable[[Engine], None] | None = None
    app_database_name: str | None = None
    default_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    )
    environment: str = "test"
    azure_ad_client_id: str = "e2e-client-id"
    azure_ad_tenant_id: str = "e2e-tenant-id"
    host_env_var: str = "E2E_HOST"
    port_env_var: str = "E2E_PORT"
    allowed_origins_env_var: str = "ALLOWED_ORIGINS"
    default_host: str = "0.0.0.0"
    default_port: int = 8099


class SharedSqlServerState:
    def __init__(self) -> None:
        self.container: Any | None = None
        self.admin_url: str | None = None


@dataclass(slots=True)
class ManagedE2eDatabases:
    admin_url: str
    app_engine: Engine
    _named_engines: dict[str, Engine] = field(default_factory=dict, init=False, repr=False)

    def engine_for(self, database_name: str) -> Engine:
        engine = self._named_engines.get(database_name)
        if engine is None:
            engine = create_engine(
                self.app_engine.url.set(database=database_name),
                future=True,
                pool_pre_ping=True,
            )
            self._named_engines[database_name] = engine
        return engine

    def dispose(self) -> None:
        for engine in self._named_engines.values():
            engine.dispose()
        self.app_engine.dispose()


def is_xdist_controller(config: Config) -> bool:
    num_processes = getattr(config.option, "numprocesses", None)
    return not hasattr(config, "workerinput") and bool(num_processes)


def _build_pyodbc_url(base_pymssql_url: str) -> str:
    """Helper to convert testcontainers default pymssql string into a valid pyodbc URL."""
    pyodbc_url = base_pymssql_url.replace("mssql+pymssql://", "mssql+pyodbc://")
    return (
        f"{pyodbc_url}"
        "?driver=ODBC+Driver+18+for+SQL+Server"
        "&TrustServerCertificate=yes"
    )


def start_controller_container(state: SharedSqlServerState, fixture_config: SqlServerFixtureConfig) -> str:
    if state.container is not None and state.admin_url is not None:
        return state.admin_url

    from testcontainers.mssql import SqlServerContainer

    # Keep default dialect here so testcontainers can safely check health natively via pymssql internally
    container = SqlServerContainer(
        image=fixture_config.image,
        password=fixture_config.password,
        dbname="master",
    )

    # Start the container FIRST before requesting network/port strings
    container.start()

    # Generate the safe pyodbc production-ready connection string
    connection_url = _build_pyodbc_url(container.get_connection_url())

    state.container = container
    state.admin_url = connection_url
    return state.admin_url


def stop_controller_container(state: SharedSqlServerState) -> None:
    if state.container is not None:
        state.container.stop()
    state.container = None
    state.admin_url = None


def configure_worker_node(
        node: WorkerController,
        *,
        state: SharedSqlServerState,
        fixture_config: SqlServerFixtureConfig,
) -> None:
    admin_url = start_controller_container(state, fixture_config)
    worker_id = node.gateway.id
    node.workerinput["shared_mssql_admin_url"] = admin_url
    node.workerinput["shared_mssql_db_name"] = f"{fixture_config.database_prefix}_{worker_id}"


def create_mssql_engine(request: pytest.FixtureRequest, fixture_config: SqlServerFixtureConfig) -> Generator[Engine]:
    config = request.config
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")

    # Mode A: Running via xdist worker nodes
    if hasattr(config, "workerinput"):
        admin_url = config.workerinput["shared_mssql_admin_url"]
        database_name = config.workerinput["shared_mssql_db_name"]

        database_url = create_database(admin_url, database_name)
        engine = create_engine(database_url, future=True, pool_pre_ping=True)
        fixture_config.migration_runner(engine)
        try:
            yield engine
        finally:
            engine.dispose()
        return

    # Mode B: Running sequentially without xdist (Master mode)
    from testcontainers.mssql import SqlServerContainer

    with SqlServerContainer(
            image=fixture_config.image,
            password=fixture_config.password,
            dbname="master",
    ) as container:
        # Enforce pyodbc connection engine mapping here as well
        admin_url = _build_pyodbc_url(container.get_connection_url())
        database_name = f"{fixture_config.database_prefix}_{worker_id}"
        database_url = create_database(admin_url, database_name)

        engine = create_engine(database_url, future=True, pool_pre_ping=True)
        fixture_config.migration_runner(engine)
        try:
            yield engine
        finally:
            engine.dispose()


def create_db_session_fixture(mssql_engine: Engine, fixture_config: SqlServerFixtureConfig) -> Generator[Session]:
    SessionLocal = sessionmaker(bind=mssql_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()

    try:
        yield session
    finally:
        # Close the connection and abort outstanding uncommitted changes first
        session.rollback()
        session.close()

        # Explicit transaction context block to cleanly scrub tables after the test runs
        with mssql_engine.begin() as conn:
            for table in fixture_config.tables_to_clean:
                conn.execute(text(f"DELETE FROM [{table}]"))


def create_database(admin_url: str, db_name: str) -> str:
    """Helper to provision a dedicated child database on the shared instance with RCSI enabled."""
    master_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with master_engine.connect() as conn:
        # Drop if it exists from a dead previous session run, then recreate clean
        conn.execute(text(f"IF DB_ID('{db_name}') IS NOT NULL DROP DATABASE [{db_name}]"))
        conn.execute(text(f"CREATE DATABASE [{db_name}]"))
        # Lock in RCSI parity with production immediately on fork
        conn.execute(text(f"ALTER DATABASE [{db_name}] SET READ_COMMITTED_SNAPSHOT ON"))
    master_engine.dispose()

    # Route connection string directly to the new database catalog name
    # Using regex to swap out the /master path name for the test database fork name
    return re.sub(r"/master(\?)", f"/{db_name}\\1", admin_url)


def split_go_batches(sql: str) -> list[str]:
    return [batch.strip() for batch in re.split(r"(?im)^\s*GO\s*$", sql) if batch.strip()]


def provision_sql_server_database_from_file(
        admin_url: str,
        *,
        database_name: str,
        sql_file: Path,
        lock_name: str,
) -> None:
    script = sql_file.read_text(encoding="utf-8")
    escaped_database_name = database_name.replace("'", "''")
    escaped_lock_name = lock_name.replace("'", "''")

    master_engine = create_engine(
        admin_url,
        future=True,
        pool_pre_ping=True,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with master_engine.connect() as conn:
            lock_result = conn.exec_driver_sql(
                f"""
                DECLARE @result int;
                EXEC @result = sp_getapplock
                    @Resource = '{escaped_lock_name}',
                    @LockMode = 'Exclusive',
                    @LockOwner = 'Session',
                    @LockTimeout = 60000;
                SELECT @result AS result;
                """
            ).scalar()
            if lock_result is None or int(lock_result) < 0:
                raise RuntimeError(f"Failed to acquire setup lock for database '{database_name}'.")

            exists = conn.exec_driver_sql(f"SELECT DB_ID(N'{escaped_database_name}')").scalar()
            if exists is None:
                for batch in split_go_batches(script):
                    conn.exec_driver_sql(batch)
    finally:
        master_engine.dispose()


def ensure_sql_server_database(
        engine: Engine,
        *,
        database_name: str,
        sql_file: Path,
        lock_name: str,
) -> None:
    provision_sql_server_database_from_file(
        engine.url.set(database="master").render_as_string(hide_password=False),
        database_name=database_name,
        sql_file=sql_file,
        lock_name=lock_name,
    )


def provision_codebook_database(
        admin_url: str,
        *,
        sql_file: Path,
        lock_name: str = "grad-pylib-test-codebook",
) -> None:
    provision_sql_server_database_from_file(
        admin_url,
        database_name=CODEBOOK_DATABASE_NAME,
        sql_file=sql_file,
        lock_name=lock_name,
    )


def ensure_codebook_database(
        engine: Engine,
        *,
        sql_file: Path,
        lock_name: str = "grad-pylib-test-codebook",
) -> None:
    ensure_sql_server_database(
        engine,
        database_name=CODEBOOK_DATABASE_NAME,
        sql_file=sql_file,
        lock_name=lock_name,
    )


@contextmanager
def create_e2e_database_bundle(
        fixture_config: SqlServerFixtureConfig,
        *,
        app_database_name: str | None = None,
) -> Generator[ManagedE2eDatabases]:
    from testcontainers.mssql import SqlServerContainer

    database_name = app_database_name or f"{fixture_config.database_prefix}_e2e"
    with SqlServerContainer(
            image=fixture_config.image,
            password=fixture_config.password,
            dbname="master",
    ) as container:
        admin_url = _build_pyodbc_url(container.get_connection_url())
        app_database_url = create_database(admin_url, database_name)
        app_engine = create_engine(app_database_url, future=True, pool_pre_ping=True)
        databases = ManagedE2eDatabases(admin_url=admin_url, app_engine=app_engine)
        try:
            yield databases
        finally:
            databases.dispose()


def run_e2e_server(
        fixture_config: SqlServerFixtureConfig,
        config: E2eServerConfig,
) -> None:
    host, port = configure_e2e_environment(config)

    print("Starting SQL Server container (this can take a minute)...", flush=True)
    with create_e2e_database_bundle(fixture_config, app_database_name=config.app_database_name) as databases:
        app_engine = databases.app_engine

        print("Running migrations...", flush=True)
        fixture_config.migration_runner(app_engine)

        codebook_engine: Engine | None = None
        if config.codebook_database_name is not None:
            if config.provision_codebook_database is not None:
                print("Provisioning Codebook database...", flush=True)
                config.provision_codebook_database(app_engine)
            codebook_engine = databases.engine_for(config.codebook_database_name)

        print("Seeding E2E dataset...", flush=True)
        seed_e2e_database(app_engine, config.seed_dataset)

        app = config.build_app(app_engine, codebook_engine)

        print(f"E2E server ready at http://{host}:{port} (docs at /docs).", flush=True)
        uvicorn.run(app, host=host, port=port, access_log=True)


def build_test_app(
        *,
        create_app: Callable[[], FastAPI],
        settings_type: type[object],
        get_settings: SettingsFactory,
        session_overrides: Sequence[SessionDependencyOverride],
) -> FastAPI:
    settings_type.model_config["env_file"] = None
    get_settings.cache_clear()
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    for override in session_overrides:
        app.dependency_overrides[override.dependency] = create_session_dependency(override.engine)
    return app


def create_session_dependency(engine: Engine) -> Callable[[], Generator[Session]]:
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def _override_get_session() -> Generator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    return _override_get_session


def configure_e2e_environment(config: E2eServerConfig) -> tuple[str, int]:
    os.environ["ENVIRONMENT"] = config.environment
    os.environ["ENABLE_DEV_API_KEY"] = "true"
    os.environ["DEV_API_KEY"] = config.dev_api_key
    os.environ["AZURE_AD_CLIENT_ID"] = config.azure_ad_client_id
    os.environ["AZURE_AD_TENANT_ID"] = config.azure_ad_tenant_id
    os.environ.setdefault(config.allowed_origins_env_var, json.dumps(list(config.default_origins)))

    host = os.environ.get(config.host_env_var, config.default_host)
    port = int(os.environ.get(config.port_env_var, str(config.default_port)))
    return host, port


def seed_e2e_database(engine: Engine, seed_dataset: Callable[[Session], None]) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        seed_dataset(session)
    finally:
        session.close()


@asynccontextmanager
async def _noop_lifespan(_: FastAPI):
    yield
