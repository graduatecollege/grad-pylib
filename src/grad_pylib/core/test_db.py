import pytest

from grad_pylib.core.config import BaseAppSettings
from grad_pylib.core.db import build_mssql_url, resolve_database_url


def test_build_mssql_url_uses_odbc_connect_query() -> None:
    url = build_mssql_url("Driver={ODBC Driver 18 for SQL Server};Server=localhost")
    assert url.startswith("mssql+pyodbc://")
    assert "odbc_connect=" in url


def test_resolve_database_url_requires_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL must be set"):
        resolve_database_url(BaseAppSettings(database_url=None))
