import argparse
import re
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from grad_pylib.data.generate_models import (
    default_generated_models_path,
    generate_models,
)
from grad_pylib.data.migrate import run_migrations

DEFAULT_SQL_SERVER_IMAGE = "mcr.microsoft.com/mssql/server:2022-CU12-ubuntu-22.04"
DEFAULT_DATABASE_NAME = "App"
_VALID_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def sqlserver_container_class() -> type[Any]:
    from testcontainers.mssql import SqlServerContainer

    return SqlServerContainer


def build_database_url(connection_url: str, database_name: str) -> str:
    return make_url(connection_url).set(database=database_name).render_as_string(hide_password=False)


def create_database(admin_database_url: str, database_name: str) -> str:
    if not _VALID_DATABASE_NAME.fullmatch(database_name):
        raise ValueError("Database name must contain only letters, numbers, and underscores.")

    engine = create_engine(admin_database_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f"if db_id(N'{database_name}') is null create database [{database_name}]")
    finally:
        engine.dispose()

    return build_database_url(admin_database_url, database_name)


def rebuild_models(
        output_path: str | None = None,
        *,
        image: str = DEFAULT_SQL_SERVER_IMAGE,
        database_name: str = DEFAULT_DATABASE_NAME,
        schema_dir: str | None = None,
        password: str | None = None,
) -> Path:
    container_class = sqlserver_container_class()
    with container_class(image=image, **{"password": password}, dbname="tempdb", dialect="mssql+pymssql") as container:
        admin_database_url = container.get_connection_url()
        database_url = create_database(admin_database_url, database_name)
        engine: Engine = create_engine(database_url, future=True, pool_pre_ping=True)
        try:
            run_migrations(engine, schema_dir=schema_dir)
        finally:
            engine.dispose()
        generate_models(output_path=output_path, database_url=database_url)

    return Path(output_path) if output_path else default_generated_models_path()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start a temporary SQL Server, run migrations, and regenerate models.",
    )
    parser.add_argument("--output-path", help="Path for generated models output.")
    parser.add_argument("--image", default=DEFAULT_SQL_SERVER_IMAGE, help="SQL Server container image.")
    parser.add_argument(
        "--database-name",
        default=DEFAULT_DATABASE_NAME,
        help="Database name to create inside the temporary SQL Server.",
    )
    parser.add_argument("--schema-dir", help="Directory containing SQL migration files.")
    parser.add_argument("--password", help="SA password for the temporary SQL Server.")
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    output_path = rebuild_models(
        output_path=args.output_path,
        image=args.image,
        database_name=args.database_name,
        schema_dir=args.schema_dir,
        **{"password": args.password},
    )
    print(f"Generated models at {output_path}")
