from unittest.mock import MagicMock

import pyodbc
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine, make_url

from grad_pylib import sqlserver_container
from grad_pylib.sqlserver_container import build_sql_server_container_kwargs, configure_sql_server_memory


@pytest.mark.parametrize("dialect", [None, "mssql+pymssql"])
def test_container_kwargs_enforce_memory_limit_without_memory_environment_variable(dialect: str | None) -> None:
    kwargs = build_sql_server_container_kwargs(
        image="custom-image",
        password="test-password",
        dbname="master",
        dialect=dialect,
    )

    expected = {
        "image": "custom-image",
        "password": "test-password",
        "dbname": "master",
        "mem_limit": "2g",
    }
    if dialect is not None:
        expected["dialect"] = dialect
    assert kwargs == expected


@pytest.mark.parametrize("fails", [False, True], ids=["success", "configuration-error"])
def test_configure_sql_server_memory_uses_autocommit_and_disposes_engine(
    fails: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock(spec=Engine)
    connection = MagicMock(spec=Connection)
    engine.connect.return_value.__enter__.return_value = connection
    create = MagicMock(return_value=engine)
    error = RuntimeError("configuration failed")
    execute = MagicMock(side_effect=error if fails else None)
    monkeypatch.setattr(sqlserver_container, "create_engine", create)
    monkeypatch.setattr(sqlserver_container, "execute_script_batch", execute)
    monkeypatch.setattr(pyodbc, "pooling", True)

    if fails:
        with pytest.raises(RuntimeError) as raised:
            configure_sql_server_memory("admin-url")
        assert raised.value is error
    else:
        configure_sql_server_memory("admin-url")

    create.assert_called_once_with("admin-url", isolation_level="AUTOCOMMIT")
    execute.assert_called_once()
    assert execute.call_args.args[0] is connection
    assert " ".join(execute.call_args.args[1].split()) == (
        "EXEC sys.sp_configure 'show advanced options', 1; RECONFIGURE; "
        "EXEC sys.sp_configure 'max server memory (MB)', 1024; RECONFIGURE;"
    )
    engine.dispose.assert_called_once_with()
    assert pyodbc.pooling is False


@pytest.mark.parametrize("driver", ["pyodbc", "pymssql"])
def test_sql_server_memory_limit_is_active_at_startup_and_reconfiguration_is_idempotent(
    mssql_admin_url: str, driver: str,
) -> None:
    url = make_url(mssql_admin_url)
    if driver == "pymssql":
        url = url.set(drivername="mssql+pymssql", query={})
    admin_url = url.render_as_string(hide_password=False)
    engine = create_engine(admin_url)
    try:
        for reconfigure in (False, True):
            if reconfigure:
                configure_sql_server_memory(admin_url)
            with engine.connect() as connection:
                configured, active = connection.exec_driver_sql(
                    "SELECT CAST(value AS int), CAST(value_in_use AS int) "
                    "FROM sys.configurations WHERE name = 'max server memory (MB)'"
                ).one()
            assert (configured, active) == (1024, 1024)
    finally:
        engine.dispose()
