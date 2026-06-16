"""Reusable SQL Server pytest fixture helpers."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from xdist.workermanage import WorkerController

from grad_pylib.data.rebuild_models import DEFAULT_SQL_SERVER_IMAGE, create_database


@dataclass(frozen=True, slots=True)
class SqlServerFixtureConfig:
    migration_runner: Callable[[Engine], None]
    tables_to_clean: tuple[str, ...]
    image: str = DEFAULT_SQL_SERVER_IMAGE
    password: str = "Test@12345!"
    database_prefix: str = "AppTest"


class SharedSqlServerState:
    def __init__(self) -> None:
        self.container: Any | None = None
        self.admin_url: str | None = None


def is_xdist_controller(config: pytest.Config) -> bool:
    num_processes = getattr(config.option, "numprocesses", None)
    return not hasattr(config, "workerinput") and bool(num_processes)


def start_controller_container(state: SharedSqlServerState, fixture_config: SqlServerFixtureConfig) -> str:
    if state.container is not None and state.admin_url is not None:
        return state.admin_url

    from testcontainers.mssql import SqlServerContainer

    state.container = SqlServerContainer(
        image=fixture_config.image,
        password=fixture_config.password,
        dbname="tempdb",
        dialect="mssql+pymssql",
    )
    state.container.start()
    state.admin_url = state.container.get_connection_url()
    return state.admin_url or ""


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


def create_mssql_engine(request: pytest.FixtureRequest, fixture_config: SqlServerFixtureConfig):
    config = request.config
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")

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

    from testcontainers.mssql import SqlServerContainer

    with SqlServerContainer(
            image=fixture_config.image,
            password=fixture_config.password,
            dbname="tempdb",
            dialect="mssql+pymssql",
    ) as container:
        admin_url = container.get_connection_url()
        database_name = f"{fixture_config.database_prefix}_{worker_id}"
        database_url = create_database(admin_url, database_name)
        engine = create_engine(database_url, future=True, pool_pre_ping=True)

        fixture_config.migration_runner(engine)
        yield engine
        engine.dispose()


def create_db_session_fixture(mssql_engine: Engine, fixture_config: SqlServerFixtureConfig):
    session: Session = sessionmaker(bind=mssql_engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    try:
        yield session
    finally:
        session.rollback()
        session.close()

    with mssql_engine.begin() as conn:
        for table in fixture_config.tables_to_clean:
            conn.execute(text(f"DELETE FROM [{table}]"))
