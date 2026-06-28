from typing import Any, get_origin, Union, get_args

from pydantic import BaseModel
from sqlalchemy import Table
from sqlalchemy.engine import CursorResult
from sqlalchemy.engine import Row
from sqlalchemy.orm import DeclarativeBase

type _RowSection = tuple[str, ...] | type[DeclarativeBase] | DeclarativeBase | Table


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


def map_row_to_pydantic[T: BaseModel](
        row: Row,
        target_model: type[T],
        nest_mappings: dict[str, Any]
) -> T:
    """
    Generically maps an un-aliased SQLAlchemy Core Row into a nested Pydantic model.
    Guaranteed collision-proof when multiple tables share duplicate column names.
    """
    payload = {}
    model_fields = target_model.model_fields

    # 1. Extract data safely on a per-table basis using table schema boundaries
    for field_name, table_obj in nest_mappings.items():
        if field_name not in model_fields:
            continue

        field_info = model_fields[field_name]
        field_type = field_info.annotation

        # Handle Union types like 'Subcollege | None'
        if get_origin(field_type) is Union or get_origin(field_type) is Any:
            sub_model_types = [arg for arg in get_args(field_type) if
                               isinstance(arg, type) and issubclass(arg, BaseModel)]
            sub_model_cls = sub_model_types[0] if sub_model_types else None
        else:
            sub_model_cls = field_type if isinstance(field_type, type) and issubclass(field_type, BaseModel) else None

        if not sub_model_cls:
            continue

        nested_data = {}
        has_valid_data = False

        # Pull data by matching the exact Column proxy context from the row object
        for col in table_obj.c:
            if col in row._mapping:
                val = row._mapping[col]
                nested_data[col.name] = val
                if val is not None:
                    has_valid_data = True

        if has_valid_data:
            payload[field_name] = sub_model_cls.model_validate(nested_data)
        else:
            payload[field_name] = None

    # 2. Extract leftover unmapped columns directly to the root payload (if any)
    all_mapped_cols = {col for table in nest_mappings.values() for col in table.c}
    for col, value in row._mapping.items():
        if col not in all_mapped_cols and hasattr(col, 'name'):
            payload[col.name] = value

    return target_model.model_validate(payload)
