from collections.abc import Mapping, Sequence
from typing import Any, Protocol, Union, get_args, get_origin

from pydantic import BaseModel
from sqlalchemy import Table
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.engine import Row
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.selectable import FromClause

from grad_pylib.core.db import _SKIP_ROWCOUNTS_OPTION, _sqlalchemy_cursor_errors

type _RowSection = tuple[str, ...] | type[DeclarativeBase] | DeclarativeBase | Table


class _RowCursor(Protocol):
    @property
    def description(self) -> Sequence[tuple[str, *tuple[object, ...]]] | None: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...


class _MultiResultCursor(_RowCursor, Protocol):
    def nextset(self) -> bool | None: ...


def qualified_columns(alias: str, section: _RowSection) -> str:
    """Qualify physical column names using SQL Server identifier quoting.

    ``alias`` must be trusted SQL, never request input. Tuple sections supply
    physical names directly; model/table sections use database names, not keys.
    """
    if isinstance(section, tuple):
        names = section
    else:
        table = section if isinstance(section, Table) else section.__table__
        names = tuple(column.name for column in table.columns)
    return ",\n            ".join(f"{alias}.[{name.replace(']', ']]')}]" for name in names)


def section_columns(section: _RowSection) -> tuple[str, ...]:
    """Return ordered tuple names or SQLAlchemy column collection keys.

    Model classes and instances use their table's keys, not ORM attribute names
    or Pydantic field names. Physical names may differ; see ``qualified_columns``.
    """
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
    becomes ``None`` rather than a dict of nulls. This is a value-based heuristic,
    not a primary-key check: a matched row of entirely nullable columns also
    becomes ``None``. Output dictionaries use ``section_columns`` keys.

    Raises ``ValueError`` if the row width does not match the combined sections.
    """
    column_sections = tuple(section_columns(section) for section in sections)
    expected_width = sum(len(columns) for columns in column_sections)
    if len(row_values) != expected_width:
        raise ValueError(
            f"Row width mismatch: expected {expected_width} values, got {len(row_values)}."
        )

    results: list[dict[str, Any] | None] = []
    offset = 0
    for columns in column_sections:
        values = row_values[offset:offset + len(columns)]
        offset += len(columns)
        if all(value is None for value in values):
            results.append(None)
            continue
        results.append(dict(zip(columns, values, strict=True)))
    return tuple(results)


def read_all_result_sets(result: Result[*tuple[Any, ...]]) -> list[list[dict[str, Any]]]:
    """Read every result of an unconsumed ``CursorResult`` using a driver supporting ``nextset``.

    Accepts ``Result`` because ``Connection.execute``/``Session.execute`` are
    typed that way; the runtime object must be a ``CursorResult`` (as for Core
    and textual statements), otherwise ``TypeError`` is raised, e.g. for ORM
    entity results. A missing/closed cursor raises ``ValueError``; a DML-only
    batch executed with ``mssql_skip_rowcounts`` has none, since it was fully
    traversed during execution.

    Each result set becomes a list of dictionaries keyed by cursor column names,
    in batch order; empty SELECTs become empty lists. Results without a column
    description, such as DML row counts, also become empty lists unless the
    execution used the ``mssql_skip_rowcounts`` option, in which case they are
    omitted. Every result is traversed, so errors raised by later statements
    in the batch are reported rather than lost.

    Driver failures while reading are raised as the matching SQLAlchemy
    ``DBAPIError`` subclass with the driver error in ``.orig``, after
    SQLAlchemy's usual error handling (``handle_error`` listeners, invalidation
    of lost connections). Other exceptions, such as row width mismatches, keep
    their identity. No partial data is returned, the reader never retries, and
    the transaction is left to its owner. The result and its cursor are always
    closed; as with any SQLAlchemy result, a cursor close failure is logged as
    "Error closing cursor", not raised.
    """
    try:
        if not isinstance(result, CursorResult):
            raise TypeError(
                f"Expected a CursorResult, got {type(result).__name__}; "
                "ORM entity results are not supported."
            )
        cursor: _MultiResultCursor | None = result.cursor
        if cursor is None:
            raise ValueError("Result has no active cursor.")
        omit_non_row_results = result.context.execution_options.get(_SKIP_ROWCOUNTS_OPTION) is True
        # No cursor is passed so the adapter leaves closing to `result.close()`; pyodbc rejects a second close.
        with _sqlalchemy_cursor_errors(result.connection, None, context=result.context):
            return _read_result_sets(cursor, omit_non_row_results)
    finally:
        result.close()


def _read_result_sets(cursor: _MultiResultCursor, omit_non_row_results: bool) -> list[list[dict[str, Any]]]:
    result_sets: list[list[dict[str, Any]]] = []
    while True:
        if not (omit_non_row_results and cursor.description is None):
            result_sets.append(cursor_rows_to_dicts(cursor))
        if not cursor.nextset():
            return result_sets


def cursor_rows_to_dicts(cursor: _RowCursor) -> list[dict[str, Any]]:
    """Read the current DB-API result set without advancing or closing the cursor.

    Output keys are the names in ``description``; use SQL aliases to distinguish
    duplicate names. A non-row-producing set has no description and returns
    ``[]``. Row widths must match the description.
    """
    description = cursor.description
    if description is None:
        return []
    columns = tuple(column[0] for column in description)
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _resolve_nested_model(field_name: str, annotation: object) -> type[BaseModel] | None:
    candidates = get_args(annotation) if get_origin(annotation) is Union else (annotation,)
    models = [
        candidate for candidate in candidates
        if isinstance(candidate, type) and issubclass(candidate, BaseModel)
    ]
    if len(models) > 1:
        raise TypeError(
            f"Nested field '{field_name}' cannot use a union of multiple Pydantic models; "
            "declare a single model type, optionally with None."
        )
    return models[0] if models else None


def map_row_to_pydantic[T: BaseModel](
        row: Row[*tuple[Any, ...]],
        target_model: type[T],
        nest_mappings: Mapping[str, FromClause],
) -> T:
    """Map a Core row into nested models and remaining root fields.

    Supply selected table columns, not rows containing ORM entity instances.
    Use the same table or SQLAlchemy alias objects in the SELECT and
    ``nest_mappings``: nested columns are matched by identity, not by name.
    For textual queries, supply ``text(...).columns(...)`` metadata for nesting.

    Nested payloads use physical ``Column.name`` values, not ``Column.key`` or
    ORM attribute names. Pydantic fields must accept those names, directly or
    through validation aliases. Root fields use result names, including labels;
    nested fields take precedence over conflicting root names.

    All-null nested sections become ``None`` based on selected values, not
    primary keys. Include a non-nullable column to distinguish unmatched joins
    from matched rows containing only nulls. Missing mapped columns are omitted.
    Unknown fields and fields without a model annotation are skipped. A union
    with multiple model types raises ``TypeError`` rather than choosing one;
    a single model with ``None`` is supported.
    """
    payload: dict[str, Any] = {}
    model_fields = target_model.model_fields
    mapping = row._mapping

    for field_name, table_obj in nest_mappings.items():
        if field_name not in model_fields:
            continue

        sub_model_cls = _resolve_nested_model(
            field_name, model_fields[field_name].annotation
        )

        if sub_model_cls is None:
            continue

        nested_data = {col.name: mapping[col] for col in table_obj.c if col in mapping}
        payload[field_name] = (
            sub_model_cls.model_validate(nested_data)
            if any(value is not None for value in nested_data.values())
            else None
        )

    # Row's public API lacks column-to-position lookup for same-named columns.
    nested_indexes = {
        row._key_to_index[col]
        for table in nest_mappings.values()
        for col in table.c
        if col in mapping
    }
    for index, (name, value) in enumerate(zip(row._fields, row, strict=True)):
        if index not in nested_indexes and name not in payload:
            payload[name] = value

    return target_model.model_validate(payload)
