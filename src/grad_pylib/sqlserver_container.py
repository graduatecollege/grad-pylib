from typing import Any

from sqlalchemy import create_engine

from grad_pylib.core.db import disable_pyodbc_pooling
from grad_pylib.core.multiquery import execute_script_batch

DEFAULT_SQL_SERVER_IMAGE = "mcr.microsoft.com/mssql/server:2022-CU12-ubuntu-22.04"
DEFAULT_SQL_SERVER_CONTAINER_MEMORY_LIMIT = "2g"
DEFAULT_SQL_SERVER_MEMORY_LIMIT_MB = 1024


def build_sql_server_container_kwargs(
        *,
        image: str = DEFAULT_SQL_SERVER_IMAGE,
        password: str | None = None,
        dbname: str,
        dialect: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "image": image,
        "password": password,
        "dbname": dbname,
        "mem_limit": DEFAULT_SQL_SERVER_CONTAINER_MEMORY_LIMIT,
    }
    if dialect is not None:
        kwargs["dialect"] = dialect
    return kwargs


def configure_sql_server_memory(admin_url: str) -> None:
    """Cap SQL Server caches, leaving headroom within the container's memory limit."""
    disable_pyodbc_pooling()
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            execute_script_batch(connection, f"""
                EXEC sys.sp_configure 'show advanced options', 1;
                RECONFIGURE;
                EXEC sys.sp_configure 'max server memory (MB)', {DEFAULT_SQL_SERVER_MEMORY_LIMIT_MB};
                RECONFIGURE;
            """)
    finally:
        engine.dispose()
