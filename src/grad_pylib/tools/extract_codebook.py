"""Extract a minimal, committable subset of the Codebook database.

The Codebook database is a large, read-only reference database that lives on a
remote SQL Server and is reached cross-database (for example
``Codebook.dbo.departments``).  Integration tests run against a throwaway SQL
Server testcontainer that has no access to it, so this script captures just
enough of Codebook -- a couple of departments and everything related to them --
into a single SQL file that can be committed to version control and replayed
into the test container.

``Codebook.dbo`` is made up entirely of *views* over the ``edw`` schema.  Rather
than reproducing the whole ``edw`` structure, each view we care about is
materialized here as a plain table with the same columns and a filtered slice of
rows.  Tests query ``Codebook.dbo.<name>`` exactly as they do in production and
do not care whether the object is a view or a table.

The script is rerunnable: every run reflects the current column layout from the
live views (so schema changes are picked up automatically) and overwrites the
output file.  Codebook data is entirely public, so there are no data-security
concerns with committing it.
"""

import argparse
import datetime
import decimal
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine

from grad_pylib.core.db import resolve_database_url

_GO_SPLIT = re.compile(r"^\s*go\s*$", re.IGNORECASE | re.MULTILINE)

CODEBOOK_DATABASE_NAME = "Codebook"
CODEBOOK_SCHEMA = "dbo"

# Departments captured when no explicit codes are supplied:
# 1434 = Computer Science, 1570 = Special Education.
DEFAULT_DEPARTMENT_CODES: tuple[str, ...] = ("1434", "1570")

# Views that are materialized into the minimal Codebook, in dependency order so
# that the generated file can be replayed top to bottom.
_VIEWS: tuple[str, ...] = (
    "departments",
    "department_codes",
    "department_names",
    "colleges",
    "subcolleges",
    "subjects",
    "grad_programs",
    "grad_program_codes",
    "joint_programs",
    "degrees",
    "curriculums",
    "majors",
    "program_names",
    "minors",
    "major_concentrations",
    "program_concentrations",
    "concentrations",
)


def codebook_sql_path() -> Path:
    return Path.cwd() / "src" / "data" / "generated" / "codebook_minimal.sql"


def _column_definitions(connection: Connection, view_name: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
            "NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :name "
            "ORDER BY ORDINAL_POSITION"
        ),
        {"schema": CODEBOOK_SCHEMA, "name": view_name},
    ).mappings()
    return [dict(row) for row in rows]


def _fetch_rows(
    connection: Connection,
    view_name: str,
    *,
    column: str | None = None,
    values: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch rows from a view, optionally filtered by ``column IN values``.

    An empty ``values`` collection short-circuits to no rows so that callers can
    pass collected foreign keys without special-casing the empty case.
    """
    print(f"Fetching {view_name} rows with {column=} {values=!r}")
    if column is not None and not values:
        return []

    sql = f"SELECT * FROM {CODEBOOK_SCHEMA}.{view_name}"
    params: dict[str, Any] = {}
    statement = text(sql)
    if column is not None:
        statement = text(f"{sql} WHERE {column} IN :values").bindparams(
            bindparam("values", expanding=True)
        )
        params["values"] = list(values or [])

    executed = connection.execute(statement, params)

    results = [dict(row) for row in executed.mappings()]
    print(f"Extracted {len(results)} rows for {view_name}")
    return results


def _collect(rows: Sequence[dict[str, Any]], key: str) -> list[Any]:
    seen: dict[Any, None] = {}
    for row in rows:
        value = row.get(key)
        if value is not None:
            seen.setdefault(value, None)
    return list(seen)


def _merge_values(*value_groups: Sequence[Any]) -> list[Any]:
    seen: dict[Any, None] = {}
    for values in value_groups:
        for value in values:
            if value is not None:
                seen.setdefault(value, None)
    return list(seen)


def extract_codebook(
    connection: Connection, department_codes: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Collect the minimal slice of Codebook starting from ``department_codes``.

    Starting from the requested departments we walk outward to the related
    colleges, graduate programs and -- via the programs -- their degrees,
    curriculums, majors, concentrations, subjects, and the departments' minors.
    """
    departments = _fetch_rows(
        connection, "departments", column="department_code_4", values=department_codes
    )
    department_code4s = _collect(departments, "department_code_4")
    department_code3s = _collect(departments, "department_code_3")

    department_code_rows = _fetch_rows(
        connection,
        "department_codes",
        column="department_code_4",
        values=department_code4s,
    )
    department_name_rows = _fetch_rows(
        connection,
        "department_names",
        column="department_code_4",
        values=department_code4s,
    )
    subjects = _fetch_rows(
        connection, "subjects", column="department_code_4", values=department_code4s
    )
    programs = _fetch_rows(
        connection, "grad_programs", column="department_code_4", values=department_codes
    )

    college_codes = _merge_values(
        _collect(departments, "college_code"),
        _collect(programs, "college_code"),
        _collect(subjects, "college_code"),
    )
    subcollege_codes = _merge_values(
        _collect(programs, "sub_college_code"),
        _collect(subjects, "sub_college_code"),
    )
    degree_codes = _collect(programs, "degree_code")
    curriculum_codes = _collect(programs, "curriculum_code")
    program_codes = _collect(programs, "program_code")
    program_ids = _collect(programs, "pgm_id")

    colleges = _fetch_rows(connection, "colleges", column="college_code", values=college_codes)
    subcolleges = _fetch_rows(
        connection, "subcolleges", column="subcollege_code", values=subcollege_codes
    )
    grad_program_codes = _fetch_rows(
        connection, "grad_program_codes", column="program_code", values=program_codes
    )
    joint_programs = _fetch_rows(
        connection, "joint_programs", column="pgm_id", values=program_ids
    )
    degrees = _fetch_rows(connection, "degrees", column="degree_code", values=degree_codes)
    curriculums = _fetch_rows(
        connection, "curriculums", column="curriculum_code", values=curriculum_codes
    )
    majors = _fetch_rows(connection, "majors", column="program_code", values=program_codes)
    minors = _fetch_rows(connection, "minors", column="department_code_4", values=department_codes)
    program_names = _fetch_rows(
        connection, "program_names", column="program_code", values=program_codes
    )
    major_concentrations = _fetch_rows(
        connection,
        "major_concentrations",
        column="program_code",
        values=program_codes,
    )
    program_concentrations = _fetch_rows(
        connection,
        "program_concentrations",
        column="program_code",
        values=program_codes,
    )
    concentration_codes = _merge_values(
        _collect(major_concentrations, "concentration_code"),
        _collect(program_concentrations, "concentration_code"),
    )
    concentrations = _fetch_rows(
        connection,
        "concentrations",
        column="concentration_code",
        values=concentration_codes,
    )

    return {
        "departments": departments,
        "department_codes": department_code_rows,
        "department_names": department_name_rows,
        "colleges": colleges,
        "subcolleges": subcolleges,
        "subjects": subjects,
        "grad_programs": programs,
        "grad_program_codes": grad_program_codes,
        "joint_programs": joint_programs,
        "degrees": degrees,
        "curriculums": curriculums,
        "majors": majors,
        "program_names": program_names,
        "minors": minors,
        "major_concentrations": major_concentrations,
        "program_concentrations": program_concentrations,
        "concentrations": concentrations,
    }


def _format_type(column: dict[str, Any]) -> str:
    data_type = str(column["DATA_TYPE"]).lower()
    length = column.get("CHARACTER_MAXIMUM_LENGTH")
    precision = column.get("NUMERIC_PRECISION")
    scale = column.get("NUMERIC_SCALE")

    if data_type in {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}:
        size = "max" if length in (-1, None) else str(length)
        return f"{data_type}({size})"
    if data_type in {"decimal", "numeric"} and precision is not None:
        return f"{data_type}({precision}, {scale or 0})"
    return data_type


def _format_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, datetime.datetime):
        return f"'{value.isoformat(sep=' ')}'"
    if isinstance(value, (datetime.date, datetime.time)):
        return f"'{value.isoformat()}'"
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return "N'" + str(value).replace("'", "''") + "'"


def _create_table_statement(view_name: str, columns: Sequence[dict[str, Any]]) -> str:
    lines = []
    for column in columns:
        nullable = "NULL" if str(column["IS_NULLABLE"]).upper() == "YES" else "NOT NULL"
        lines.append(f"    [{column['COLUMN_NAME']}] {_format_type(column)} {nullable}")
    body = ",\n".join(lines)
    return (
        f"drop table if exists {CODEBOOK_SCHEMA}.{view_name};\n"
        f"go\n\n"
        f"create table {CODEBOOK_SCHEMA}.{view_name} (\n{body}\n);\n"
        f"go"
    )


def _insert_statements(
    view_name: str, columns: Sequence[dict[str, Any]], rows: Sequence[dict[str, Any]]
) -> str:
    if not rows:
        return f"-- no rows for {CODEBOOK_SCHEMA}.{view_name}"

    column_names = [column["COLUMN_NAME"] for column in columns]
    column_list = ", ".join(f"[{name}]" for name in column_names)
    values = [
        "    (" + ", ".join(_format_literal(row.get(name)) for name in column_names) + ")"
        for row in rows
    ]
    return (
        f"insert into {CODEBOOK_SCHEMA}.{view_name} ({column_list}) values\n"
        + ",\n".join(values)
        + ";\ngo"
    )


def generate_sql(
    columns_by_view: dict[str, list[dict[str, Any]]],
    rows_by_view: dict[str, list[dict[str, Any]]],
) -> str:
    parts = [
        "-- Minimal Codebook schema and data for integration tests.",
        "-- Generated by `make extract-codebook` - do not edit by hand.",
        "-- Codebook data is public reference data.",
        "",
        f"if db_id(N'{CODEBOOK_DATABASE_NAME}') is null create database [{CODEBOOK_DATABASE_NAME}];",
        "go",
        "",
        f"use [{CODEBOOK_DATABASE_NAME}];",
        "go",
    ]
    for view_name in _VIEWS:
        columns = columns_by_view[view_name]
        parts.append("")
        parts.append(f"-- {CODEBOOK_SCHEMA}.{view_name}")
        parts.append(_create_table_statement(view_name, columns))
        parts.append("")
        parts.append(_insert_statements(view_name, columns, rows_by_view[view_name]))
    return "\n".join(parts) + "\n"


def export_codebook(
    output_path: str | None = None,
    *,
    database_url: str | None = None,
    department_codes: Sequence[str] | None = None,
) -> Path:
    database_url = database_url or resolve_database_url()
    target_path = Path(output_path) if output_path else codebook_sql_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    codes = list(department_codes) if department_codes else list(DEFAULT_DEPARTMENT_CODES)
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            columns_by_view = {
                view_name: _column_definitions(connection, view_name) for view_name in _VIEWS
            }
            rows_by_view = extract_codebook(connection, codes)
    finally:
        engine.dispose()

    sql = generate_sql(columns_by_view, rows_by_view)
    target_path.write_text(sql, encoding="utf-8")
    return target_path


def _split_batches(sql_text: str) -> list[str]:
    return [batch.strip() for batch in _GO_SPLIT.split(sql_text) if batch.strip()]


def load_codebook(engine: Engine, sql_text: str | None = None) -> None:
    """Replay the committed minimal Codebook SQL into ``engine``.

    Intended for test fixtures: it creates the ``Codebook`` database (if needed)
    and loads its tables and data on a single connection so that the ``USE``
    statement persists across batches.
    """
    if sql_text is None:
        sql_text = codebook_sql_path().read_text(encoding="utf-8")

    original_database = engine.url.database

    raw = engine.raw_connection()
    try:
        raw.autocommit(True)
        cursor = raw.cursor()
        try:
            for batch in _split_batches(sql_text):
                cursor.execute(batch)
        finally:
            cursor.close()
    finally:
        # The script issues `USE [Codebook]`; restore the original database
        # context before returning the connection to the pool so that callers
        # reusing the engine still target their own database.
        if original_database:
            restore = raw.cursor()
            try:
                restore.execute(f"USE [{original_database}]")
            finally:
                restore.close()
        raw.close()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a minimal Codebook schema and data slice into a committable SQL file.",
    )
    parser.add_argument("--output-path", help="Path for the generated SQL file.")
    parser.add_argument("--database-url", help="Database URL override.")
    parser.add_argument(
        "--department-code",
        action="append",
        dest="department_codes",
        help=(
            "Department code to include (repeatable). "
            "Defaults to 1434 (Computer Science) and 1570 (Special Education)."
        ),
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    output_path = export_codebook(
        output_path=args.output_path,
        database_url=args.database_url,
        department_codes=args.department_codes,
    )
    print(f"Wrote minimal Codebook to {output_path}")
