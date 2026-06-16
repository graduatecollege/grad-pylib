import argparse
import hashlib
import re
from pathlib import Path

from sqlalchemy import Column, DateTime, MetaData, String, Table, func, select
from sqlalchemy.engine import Engine

from grad_pylib.core.db import get_engine

_GO_SPLIT = re.compile(r"^\s*go\s*$", re.IGNORECASE | re.MULTILINE)
_MIGRATION_HISTORY = Table(
    "schema_migrations",
    MetaData(),
    Column("file_name", String(255), primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column("applied_at", DateTime(), nullable=False, server_default=func.now()),
)


def default_schema_directory() -> Path:
    return Path.cwd() / "schema"


def split_batches(sql_text: str) -> list[str]:
    return [batch.strip() for batch in _GO_SPLIT.split(sql_text) if batch.strip()]


def checksum(sql_text: str) -> str:
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def ensure_migration_history(engine: Engine) -> None:
    _MIGRATION_HISTORY.metadata.create_all(engine, tables=[_MIGRATION_HISTORY], checkfirst=True)


def applied_migrations(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(select(_MIGRATION_HISTORY.c.file_name, _MIGRATION_HISTORY.c.checksum))
        return {row.file_name: row.checksum for row in rows}


def run_migrations(
        engine_override: Engine | None = None,
        *,
        schema_dir: str | Path | None = None,
) -> None:
    eng = engine_override or get_engine()
    schema_path = Path(schema_dir) if schema_dir else default_schema_directory()

    ensure_migration_history(eng)
    applied = applied_migrations(eng)

    for sql_file in sorted(schema_path.glob("*.sql")):
        sql_text = sql_file.read_text(encoding="utf-8-sig")
        file_checksum = checksum(sql_text)

        if applied.get(sql_file.name) == file_checksum:
            continue
        if sql_file.name in applied:
            raise RuntimeError(f"Migration {sql_file.name} has changed since it was applied.")

        with eng.begin() as connection:
            for batch in split_batches(sql_text):
                connection.exec_driver_sql(batch)
            connection.execute(
                _MIGRATION_HISTORY.insert().values(file_name=sql_file.name, checksum=file_checksum),
            )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply SQL migrations from the schema directory.")
    parser.add_argument("--schema-dir", help="Directory containing SQL migration files.")
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    run_migrations(schema_dir=args.schema_dir)
