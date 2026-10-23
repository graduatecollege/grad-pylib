"""Shared SQL Server infrastructure for grad-pylib's own tests.

One disposable SQL Server container serves the whole run: under xdist the controller starts it
and each worker receives its own database; without xdist the session fixture starts it. Tests
needing database isolation use `mssql_databases`, a fresh database per test module that is
dropped afterwards. There is deliberately no fallback to another database engine.
"""

import os
import re
from collections.abc import Generator

import pytest
from _pytest.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from xdist.workermanage import WorkerController

from grad_pylib.testing import (
    ManagedE2eDatabases,
    SqlServerBootstrapConfig,
    SqlServerFixtureConfig,
    SqlServerTestBootstrap,
    create_database,
)
from grad_pylib.testing.fixtures import _create_app_engine, _url_with_database

_DATABASE_PREFIX = "GradPyLib"
_bootstrap = SqlServerTestBootstrap(SqlServerBootstrapConfig(
    fixture_config=SqlServerFixtureConfig(
        migration_runner=lambda _engine: None,
        tables_to_clean=(),
        database_prefix=_DATABASE_PREFIX,
    ),
))


def pytest_configure(config: Config) -> None:
    _bootstrap.pytest_configure(config)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: WorkerController) -> None:
    _bootstrap.pytest_configure_node(node)


def pytest_unconfigure(config: Config) -> None:
    _bootstrap.pytest_unconfigure(config)


mssql_engine = _bootstrap.mssql_engine_fixture()


@pytest.fixture(scope="session")
def mssql_admin_url(mssql_engine: Engine) -> str:
    """Return the `master` URL of the shared SQL Server instance, for creating databases."""
    return _url_with_database(mssql_engine.url, "master").render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def mssql_databases(request: pytest.FixtureRequest, mssql_admin_url: str) -> Generator[ManagedE2eDatabases]:
    """Provide a fresh SQL Server database for one test module and drop it afterwards.

    The database is named for the xdist worker and module, so modules never share tables or data.
    Tests remain responsible for cleaning up state they commit within their module.
    """
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    module_name = re.sub(r"\W", "_", request.module.__name__.rpartition(".")[2])
    database_name = f"{_DATABASE_PREFIX}_{worker_id}_{module_name}"[:128]

    app_engine = _create_app_engine(create_database(mssql_admin_url, database_name))
    databases = ManagedE2eDatabases(admin_url=mssql_admin_url, app_engine=app_engine)
    try:
        yield databases
    finally:
        databases.dispose()
        _drop_database(mssql_admin_url, database_name)


def _drop_database(admin_url: str, database_name: str) -> None:
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            # Tests may leave sessions open, such as deliberately contended connections.
            connection.exec_driver_sql(
                f"IF DB_ID(N'{database_name}') IS NOT NULL "
                f"ALTER DATABASE [{database_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
            )
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS [{database_name}]")
    finally:
        admin_engine.dispose()
