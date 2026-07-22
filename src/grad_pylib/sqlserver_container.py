from typing import Any

DEFAULT_SQL_SERVER_IMAGE = "mcr.microsoft.com/mssql/server:2022-CU12-ubuntu-22.04"
DEFAULT_SQL_SERVER_CONTAINER_MEMORY_LIMIT = "1g"
DEFAULT_SQL_SERVER_MEMORY_LIMIT_MB = 768


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
        "env": {"MSSQL_MEMORY_LIMIT_MB": str(DEFAULT_SQL_SERVER_MEMORY_LIMIT_MB)},
        "mem_limit": DEFAULT_SQL_SERVER_CONTAINER_MEMORY_LIMIT,
    }
    if dialect is not None:
        kwargs["dialect"] = dialect
    return kwargs
