"""Reusable sorting and filtering helpers for SQLAlchemy ``select`` statements.

The goal is to keep feature services and routers thin: they only declare which
columns may be filtered and sorted via a :class:`QuerySpec`, while the generic
machinery here parses request parameters and applies the corresponding
``WHERE`` / ``ORDER BY`` clauses.

Filtering parameters use a ``field`` or ``field__operator`` naming convention,
e.g. ``status=submitted`` (equality), ``requested_amount__gte=100``, or
``reviewed_at__isnull=true``.

Sorting parameters are a comma separated list of fields, where a leading ``-``
denotes descending order, e.g. ``sort=-submitted_at,department_code``.

For raw ``text(...)`` queries, :func:`build_where_clause` can also compose
developer-supplied fixed predicates with request-driven filters while keeping
values parameterized and reporting any ``IN`` parameters that should be bound as
SQLAlchemy expanding parameters.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from grad_pylib.core.exceptions import BadRequestError
from sqlalchemy import Select, bindparam
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement, TextClause

type Column = ColumnElement[Any] | InstrumentedAttribute[Any]
type FilterOperator = Callable[[Column, Any], ColumnElement[bool]]

_COLUMN_FILTER_OPERATORS: dict[str, FilterOperator] = {
    "eq": lambda column, value: column == value,
    "ne": lambda column, value: column != value,
    "lt": lambda column, value: column < value,
    "lte": lambda column, value: column <= value,
    "gt": lambda column, value: column > value,
    "gte": lambda column, value: column >= value,
    "like": lambda column, value: column.like(value),
    "ilike": lambda column, value: column.ilike(value),
}

_RAW_FILTER_OPERATORS: dict[str, str] = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "like": "LIKE",
    "ilike": "ILIKE",
}

_NULL_FILTER_OPERATORS = frozenset({"isnull", "notnull"})
_GENERATED_PARAM_PREFIX = "__grad_pylib_filter_"


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


@dataclass(frozen=True, slots=True)
class RawWhereClause:
    """A raw SQL ``WHERE`` clause with its bind parameters.

    ``build_where_clause()`` returns this object so raw SQL callers can access
    ``sql``, ``params``, and any list-valued ``expanding_params`` directly. The
    object also preserves the previous convenience of unpacking into
    ``(sql, params)``.
    """

    sql: str
    params: dict[str, Any]
    expanding_params: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Any]:
        yield self.sql
        yield self.params

    def bind(self, query: TextClause) -> TextClause:
        """Bind this clause's parameters onto ``query``.

        Any ``IN`` parameters produced by :func:`build_where_clause` are marked as
        SQLAlchemy expanding parameters automatically.
        """
        query = bind_expanding_params(query, self.expanding_params)
        existing_bindparams = query._bindparams
        collisions = {
            name
            for name in self.params
            if name in existing_bindparams
            and existing_bindparams[name].value is not None
        }
        if collisions:
            names = ", ".join(sorted(collisions))
            raise ValueError(f"Generated query parameters already have values: {names}.")
        return query.params(**self.params)


def _parse_filter_key(key: str) -> tuple[str, str]:
    field, _, operator = key.partition("__")
    return field, operator or "eq"


def _validate_requested_fields(
        requested_fields: Iterable[str],
        allowed_fields: Mapping[str, Any],
        *,
        action: str,
) -> tuple[str, ...]:
    unique_fields = tuple(dict.fromkeys(requested_fields))
    for field in unique_fields:
        if field not in allowed_fields:
            raise BadRequestError(f"{action} by '{field}' is not supported.")
    if len(unique_fields) > len(allowed_fields):
        raise BadRequestError(
            f"Too many {action.lower()} fields requested; at most {len(allowed_fields)} field(s) are allowed."
        )
    return unique_fields


def _coerce_in_values(value: Any) -> list[Any]:
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    coerced = list(values)
    if not coerced:
        raise BadRequestError("Filter operator 'in' requires at least one value.")
    return coerced


def _coerce_bool_filter(operator: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off"}:
            return False
    raise BadRequestError(
        f"Filter operator '{operator}' requires a boolean value."
    )


def _matches_null(operator: str, value: Any) -> bool:
    wants_null = _coerce_bool_filter(operator, value)
    return wants_null if operator == "isnull" else not wants_null


def apply_filters[T: tuple[Any, ...]](
        stmt: Select[T], spec: QuerySpec, filters: Mapping[str, Any] | None
) -> Select[T]:
    """Apply ``WHERE`` clauses for the supplied filters.

    Filter keys use a ``field`` or ``field__operator`` convention. Values that
    are ``None`` are ignored so callers may pass optional query parameters
    directly. Unknown fields or operators raise :class:`BadRequestError`.
    """
    if not filters:
        return stmt
    _validate_requested_fields(
        (
            _parse_filter_key(key)[0]
            for key, value in filters.items()
            if value is not None
        ),
        spec.filterable,
        action="Filtering",
    )
    for key, value in filters.items():
        if value is None:
            continue
        field, operator = _parse_filter_key(key)
        column = spec.filterable[field]
        if operator in _NULL_FILTER_OPERATORS:
            stmt = stmt.where(
                column.is_(None)
                if _matches_null(operator, value)
                else column.is_not(None)
            )
            continue
        if operator == "in":
            stmt = stmt.where(column.in_(_coerce_in_values(value)))
            continue
        builder = _COLUMN_FILTER_OPERATORS.get(operator)
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


def apply_sort[T: tuple[Any, ...]](
        stmt: Select[T], spec: QuerySpec, sort: str | Sequence[str] | None
) -> Select[T]:
    """Apply ``ORDER BY`` clauses for the requested sort expression.

    Falls back to ``spec.default_sort`` when ``sort`` is empty. Unknown fields
    raise :class:`BadRequestError`.
    """
    effective = sort if sort else spec.default_sort
    if not effective:
        return stmt
    requested_fields = _parse_sort(effective)
    _validate_requested_fields(
        (field for field, _ in requested_fields), spec.sortable, action="Sorting"
    )
    for field, descending in requested_fields:
        column = spec.sortable[field]
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
    name = getattr(column, "name", None) or getattr(column, "key", None)
    if not isinstance(name, str) or not name:
        raise BadRequestError(f"Unable to build SQL for field '{field}'.")
    if not _IDENTIFIER_RE.match(name):
        raise BadRequestError(f"Unable to build SQL for field '{field}'.")
    return name


def bind_expanding_params(query: TextClause, param_names: Sequence[str]) -> TextClause:
    """Bind ``param_names`` on ``query`` as SQLAlchemy expanding parameters."""
    for param_name in param_names:
        query = query.bindparams(bindparam(param_name, expanding=True))
    return query


def build_where_clause(
        spec: QuerySpec,
        filters: Mapping[str, Any] | None,
        *,
        fixed_clauses: Sequence[str] = (),
) -> RawWhereClause:
    """Build a raw SQL ``WHERE`` clause and bind parameters.

    ``fixed_clauses`` lets callers prepend fixed, developer-authored predicates
    (for example, ``"term = :term"``) without manually re-joining all ``WHERE``
    fragments. ``IN`` filters always use expanding parameters so callers can bind
    the returned clause directly via :meth:`RawWhereClause.bind`.
    """
    filters = filters or {}
    clauses = [clause.strip() for clause in fixed_clauses if clause.strip()]
    if any(_GENERATED_PARAM_PREFIX in clause for clause in clauses):
        raise ValueError(
            f"Fixed clauses must not use the reserved parameter namespace "
            f"'{_GENERATED_PARAM_PREFIX}'."
        )
    if not filters and not clauses:
        return RawWhereClause("", {})

    _validate_requested_fields(
        (
            _parse_filter_key(key)[0]
            for key, value in filters.items()
            if value is not None
        ),
        spec.filterable,
        action="Filtering",
    )

    params: dict[str, Any] = {}
    expanding_params: list[str] = []
    for index, (key, value) in enumerate(filters.items(), start=1):
        if value is None:
            continue
        field, operator = _parse_filter_key(key)
        column = spec.filterable[field]

        column_name = _raw_column_name(field, column)
        operator_sql = _RAW_FILTER_OPERATORS.get(operator)

        if operator in _NULL_FILTER_OPERATORS:
            clauses.append(
                f"{column_name} {'IS NULL' if _matches_null(operator, value) else 'IS NOT NULL'}"
            )
            continue

        if operator_sql is not None:
            param_name = f"{_GENERATED_PARAM_PREFIX}{index}"
            clauses.append(f"{column_name} {operator_sql} :{param_name}")
            params[param_name] = value
            continue

        if operator == "in":
            param_name = f"{_GENERATED_PARAM_PREFIX}{index}"
            clauses.append(f"{column_name} IN :{param_name}")
            params[param_name] = _coerce_in_values(value)
            expanding_params.append(param_name)
            continue

        raise BadRequestError(f"Filter operator '{operator}' is not supported.")

    if not clauses:
        return RawWhereClause("", {})
    return RawWhereClause(
        f"WHERE {' AND '.join(f'({clause})' for clause in clauses)}",
        params,
        tuple(expanding_params),
    )


def build_order_by_clause(spec: QuerySpec, sort: str | Sequence[str] | None) -> str:
    """Build a raw SQL ``ORDER BY`` clause from ``sort`` or ``spec.default_sort``."""
    effective = sort if sort else spec.default_sort
    if not effective:
        return ""

    requested_fields = _parse_sort(effective)
    _validate_requested_fields(
        (field for field, _ in requested_fields), spec.sortable, action="Sorting"
    )

    clauses: list[str] = []
    for field, descending in requested_fields:
        column = spec.sortable[field]
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
