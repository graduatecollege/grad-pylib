"""Reusable sorting and filtering helpers for SQLAlchemy ``select`` statements.

The goal is to keep feature services and routers thin: they only declare which
columns may be filtered and sorted via a :class:`QuerySpec`, while the generic
machinery here parses request parameters and applies the corresponding
``WHERE`` / ``ORDER BY`` clauses.

Filtering parameters use a ``field`` or ``field__operator`` naming convention,
e.g. ``status=submitted`` (equality) or ``requested_amount__gte=100``.

Sorting parameters are a comma separated list of fields, where a leading ``-``
denotes descending order, e.g. ``sort=-submitted_at,department_code``.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import Select
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from grad_pylib.core.exceptions import BadRequestError

type Column = ColumnElement[Any] | InstrumentedAttribute[Any]
type FilterOperator = Callable[[Column, Any], ColumnElement[bool]]

_OPERATORS: dict[str, FilterOperator] = {
    "eq": lambda column, value: column == value,
    "ne": lambda column, value: column != value,
    "lt": lambda column, value: column < value,
    "lte": lambda column, value: column <= value,
    "gt": lambda column, value: column > value,
    "gte": lambda column, value: column >= value,
    "like": lambda column, value: column.like(value),
    "ilike": lambda column, value: column.ilike(value),
    "in": lambda column, value: column.in_(value if isinstance(value, (list, tuple, set)) else [value]),
}


class QuerySpec:
    """Declares which columns may be filtered and sorted for an endpoint/service.

    :param filterable: mapping of public filter name to the SQLAlchemy column.
    :param sortable: mapping of public sort name to the SQLAlchemy column.
    :param default_sort: sort expression applied when no sort is requested.
    """

    def __init__(
            self,
            *,
            filterable: Mapping[str, Column] | None = None,
            sortable: Mapping[str, Column] | None = None,
            default_sort: str | None = None,
    ) -> None:
        self.filterable: dict[str, Column] = dict(filterable or {})
        self.sortable: dict[str, Column] = dict(sortable or {})
        self.default_sort = default_sort


def _parse_filter_key(key: str) -> tuple[str, str]:
    field, _, operator = key.partition("__")
    return field, operator or "eq"


def apply_filters[T: tuple[Any, ...]](stmt: Select[T], spec: QuerySpec, filters: Mapping[str, Any] | None) -> Select[T]:
    """Apply ``WHERE`` clauses for the supplied filters.

    Filter keys use a ``field`` or ``field__operator`` convention. Values that
    are ``None`` are ignored so callers may pass optional query parameters
    directly. Unknown fields or operators raise :class:`BadRequestError`.
    """
    if not filters:
        return stmt
    requested_fields = {_parse_filter_key(key)[0] for key, value in filters.items() if value is not None}
    if len(requested_fields) > len(spec.filterable):
        raise BadRequestError(
            f"Too many filter fields requested; at most {len(spec.filterable)} field(s) are allowed."
        )
    for key, value in filters.items():
        if value is None:
            continue
        field, operator = _parse_filter_key(key)
        column = spec.filterable.get(field)
        if column is None:
            raise BadRequestError(f"Filtering by '{field}' is not supported.")
        builder = _OPERATORS.get(operator)
        if builder is None:
            raise BadRequestError(f"Filter operator '{operator}' is not supported.")
        stmt = stmt.where(builder(column, value))
    return stmt


def _parse_sort(sort: str | Sequence[str]) -> list[tuple[str, bool]]:
    tokens = sort.split(",") if isinstance(sort, str) else list(sort)
    parsed: list[tuple[str, bool]] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        descending = token.startswith("-")
        field = token[1:] if descending else token
        parsed.append((field.strip(), descending))
    return parsed


def apply_sort[T: tuple[Any, ...]](stmt: Select[T], spec: QuerySpec, sort: str | Sequence[str] | None) -> Select[T]:
    """Apply ``ORDER BY`` clauses for the requested sort expression.

    Falls back to ``spec.default_sort`` when ``sort`` is empty. Unknown fields
    raise :class:`BadRequestError`.
    """
    effective = sort if sort else spec.default_sort
    if not effective:
        return stmt
    requested_fields = _parse_sort(effective)
    if len({field for field, _ in requested_fields}) > len(spec.sortable):
        raise BadRequestError(
            f"Too many sort fields requested; at most {len(spec.sortable)} field(s) are allowed."
        )
    for field, descending in requested_fields:
        column = spec.sortable.get(field)
        if column is None:
            raise BadRequestError(f"Sorting by '{field}' is not supported.")
        stmt = stmt.order_by(column.desc() if descending else column.asc())
    return stmt


# A SQL identifier (optionally schema/table qualified, e.g. ``dbo.table.column``).
# Each dotted segment must be a plain identifier: starts with a letter or
# underscore, followed by letters, digits, or underscores.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _raw_column_name(field: str, column: Column) -> str:
    """Resolve the raw SQL identifier for ``column`` and validate it.

    The column name must originate from a developer-declared :class:`QuerySpec`,
    never from request data. As defense-in-depth, the resolved identifier is
    validated against :data:`_IDENTIFIER_RE` so a future misconfiguration (e.g.
    a spec built from untrusted input) cannot produce injectable SQL that is
    later interpolated into a raw ``text(...)`` statement.
    """
    name = getattr(column, "key", None) or getattr(column, "name", None)
    if not isinstance(name, str) or not name:
        raise BadRequestError(f"Unable to build SQL for field '{field}'.")
    if not _IDENTIFIER_RE.match(name):
        raise BadRequestError(f"Unable to build SQL for field '{field}'.")
    return name


def build_where_clause(spec: QuerySpec, filters: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Build a raw SQL ``WHERE`` clause and bind parameters from ``filters``."""
    if not filters:
        return "", {}

    requested_fields = {_parse_filter_key(key)[0] for key, value in filters.items() if value is not None}
    if len(requested_fields) > len(spec.filterable):
        raise BadRequestError(
            f"Too many filter fields requested; at most {len(spec.filterable)} field(s) are allowed."
        )

    params: dict[str, Any] = {}
    clauses: list[str] = []
    for index, (key, value) in enumerate(filters.items(), start=1):
        if value is None:
            continue
        field, operator = _parse_filter_key(key)
        column = spec.filterable.get(field)
        if column is None:
            raise BadRequestError(f"Filtering by '{field}' is not supported.")

        column_name = _raw_column_name(field, column)
        operator_sql = {
            "eq": "=",
            "ne": "!=",
            "lt": "<",
            "lte": "<=",
            "gt": ">",
            "gte": ">=",
            "like": "LIKE",
            "ilike": "ILIKE",
        }.get(operator)

        if operator_sql is not None:
            param_name = f"{field}_{index}"
            clauses.append(f"{column_name} {operator_sql} :{param_name}")
            params[param_name] = value
            continue

        if operator == "in":
            values = value if isinstance(value, (list, tuple, set)) else [value]
            values = list(values)
            if not values:
                raise BadRequestError("Filter operator 'in' requires at least one value.")
            placeholders: list[str] = []
            for item_index, item in enumerate(values, start=1):
                param_name = f"{field}_{index}_{item_index}"
                placeholders.append(f":{param_name}")
                params[param_name] = item
            clauses.append(f"{column_name} IN ({', '.join(placeholders)})")
            continue

        raise BadRequestError(f"Filter operator '{operator}' is not supported.")

    if not clauses:
        return "", {}
    return f"WHERE {' AND '.join(clauses)}", params


def build_order_by_clause(spec: QuerySpec, sort: str | Sequence[str] | None) -> str:
    """Build a raw SQL ``ORDER BY`` clause from ``sort`` or ``spec.default_sort``."""
    effective = sort if sort else spec.default_sort
    if not effective:
        return ""

    requested_fields = _parse_sort(effective)
    if len({field for field, _ in requested_fields}) > len(spec.sortable):
        raise BadRequestError(
            f"Too many sort fields requested; at most {len(spec.sortable)} field(s) are allowed."
        )

    clauses: list[str] = []
    for field, descending in requested_fields:
        column = spec.sortable.get(field)
        if column is None:
            raise BadRequestError(f"Sorting by '{field}' is not supported.")
        direction = "DESC" if descending else "ASC"
        clauses.append(f"{_raw_column_name(field, column)} {direction}")

    if not clauses:
        return ""
    return f"ORDER BY {', '.join(clauses)}"


def _coerce_int(name: str, value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"'{name}' must be an integer.") from exc


def apply_pagination[T: tuple[Any, ...]](
        stmt: Select[T],
        *,
        limit: int | str | None = None,
        offset: int | str | None = None,
) -> Select[T]:
    """Apply ``LIMIT``/``OFFSET`` clauses.

    ``limit`` must be a positive integer when provided. ``offset`` must be a
    non-negative integer when provided.
    """
    parsed_limit = _coerce_int("limit", limit)
    parsed_offset = _coerce_int("offset", offset)

    if parsed_limit is not None:
        if parsed_limit <= 0:
            raise BadRequestError("'limit' must be greater than 0.")
        stmt = stmt.limit(parsed_limit)
    if parsed_offset is not None:
        if parsed_offset < 0:
            raise BadRequestError("'offset' must be greater than or equal to 0.")
        stmt = stmt.offset(parsed_offset)
    return stmt


def apply_query[T: tuple[Any, ...]](
        stmt: Select[T],
        spec: QuerySpec,
        *,
        filters: Mapping[str, Any] | None = None,
        sort: str | Sequence[str] | None = None,
        limit: int | str | None = None,
        offset: int | str | None = None,
) -> Select[T]:
    """Apply filtering, sorting, and pagination to ``stmt`` based on ``spec``."""
    stmt = apply_filters(stmt, spec, filters)
    stmt = apply_sort(stmt, spec, sort)
    stmt = apply_pagination(stmt, limit=limit, offset=offset)
    return stmt
