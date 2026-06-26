from typing import Any

from sqlalchemy import Table
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import DeclarativeBase

type _RowSection = tuple[str, ...] | DeclarativeBase | Table

def qualified_columns(alias: str, section: _RowSection) -> str:
    # Column names come from model/table metadata (never user input), so direct
    # interpolation here is safe; the request parameters remain fully parameterized.
    return ",\n            ".join(f"{alias}.[{column}]" for column in section_columns(section))


def section_columns(section: _RowSection) -> tuple[str, ...]:
    if isinstance(section, tuple):
        return section
    if isinstance(section, Table):
        return tuple(section.c.keys())
    return tuple(section.__table__.columns.keys())


def split_row_sections(
        row_values: tuple[Any, ...],
        *sections: _RowSection,
) -> tuple[dict[str, Any] | None, ...]:
    """Split a concatenated row back into per-section column dicts.

    Consumes ``row_values`` in order, slicing off the column count of each
    section. A section whose values are all ``None`` (an unmatched LEFT JOIN)
    becomes ``None`` rather than a dict of nulls.
    """
    results: list[dict[str, Any] | None] = []
    offset = 0
    for section in sections:
        columns = section_columns(section)
        values = row_values[offset:offset + len(columns)]
        offset += len(columns)
        if all(value is None for value in values):
            results.append(None)
            continue
        results.append({column: value for column, value in zip(columns, values, strict=False)})
    return tuple(results)


def read_all_result_sets(result: CursorResult[Any]) -> list[list[dict[str, Any]]]:
    cursor = result.cursor
    try:
        result_sets = [cursor_rows_to_dicts(cursor)]
        while cursor.nextset():
            result_sets.append(cursor_rows_to_dicts(cursor))
        return result_sets
    finally:
        result.close()


def cursor_rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    if cursor.description is None:
        return []
    columns = tuple(column[0] for column in cursor.description)
    return [{column: value for column, value in zip(columns, row, strict=False)} for row in cursor.fetchall()]
