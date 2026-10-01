"""Request models that declare ``field__operator`` filters for :mod:`grad_pylib.core.querying`.

Each filterable field explicitly lists the operators it supports, so only those
operators are validated, typed, and documented in the OpenAPI spec. The
``eq`` operator is exposed as the bare field name.

Example::

    StudentListFilters = create_filter_model(
        "StudentListFilters",
        {
            "college_code": FilterField(str, "eq", "in", "isnull"),
            "deposit_date": FilterField(date, "gte", "lte", "isnull"),
        },
    )
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, create_model

from grad_pylib.core.schemas import BaseDto

type FilterOperatorName = Literal[
    "eq", "ne", "lt", "lte", "gt", "gte", "like", "ilike", "in", "isnull", "notnull"
]

FILTER_OPERATORS: tuple[FilterOperatorName, ...] = get_args(FilterOperatorName.__value__)

_STRING_ONLY_OPERATORS = frozenset({"like", "ilike"})
_BOOLEAN_OPERATORS = frozenset({"isnull", "notnull"})

_OPERATOR_DESCRIPTIONS: dict[str, str] = {
    "eq": "Equal to",
    "ne": "Not equal to",
    "lt": "Less than",
    "lte": "Less than or equal to",
    "gt": "Greater than",
    "gte": "Greater than or equal to",
    "like": "SQL LIKE pattern (use % and _ wildcards)",
    "ilike": "Case-insensitive SQL LIKE pattern (use % and _ wildcards)",
    "in": "Equal to any of the values",
    "isnull": "When true, only rows where the value is null; when false, only non-null",
    "notnull": "When true, only rows where the value is not null; when false, only null",
}


@dataclass(frozen=True, slots=True, init=False)
class FilterField:
    """A filterable field with its value type and the operators it supports."""

    value_type: type
    operators: tuple[FilterOperatorName, ...]
    description: str | None

    def __init__(
            self,
            value_type: type,
            *operators: FilterOperatorName,
            description: str | None = None,
    ) -> None:
        if not operators:
            raise ValueError("FilterField requires at least one operator.")
        unknown = [operator for operator in operators if operator not in FILTER_OPERATORS]
        if unknown:
            raise ValueError(f"Unsupported filter operators: {', '.join(unknown)}.")
        if value_type is not str and (string_only := _STRING_ONLY_OPERATORS.intersection(operators)):
            raise ValueError(
                f"Operators {', '.join(sorted(string_only))} require a str field."
            )
        object.__setattr__(self, "value_type", value_type)
        object.__setattr__(self, "operators", tuple(dict.fromkeys(operators)))
        object.__setattr__(self, "description", description)


def filter_key(field: str, operator: FilterOperatorName) -> str:
    """Return the request key for ``field`` and ``operator`` (``eq`` is the bare field)."""
    return field if operator == "eq" else f"{field}__{operator}"


def filter_field_definitions(fields: Mapping[str, FilterField]) -> dict[str, Any]:
    """Build pydantic field definitions for each declared field/operator pair."""
    definitions: dict[str, Any] = {}
    for name, field in fields.items():
        for operator in field.operators:
            if operator in _BOOLEAN_OPERATORS:
                annotation: Any = bool | None
            elif operator == "in":
                # incorrectly flags as non-type
                # noinspection type-hints
                annotation = list[field.value_type] | None
            else:
                annotation = field.value_type | None
            description = _OPERATOR_DESCRIPTIONS[operator]
            if field.description:
                description = f"{field.description}: {description[0].lower()}{description[1:]}"
            definitions[filter_key(name, operator)] = (
                annotation,
                Field(default=None, description=description),
            )
    return definitions


def create_filter_model[M: BaseModel](
        model_name: str,
        fields: Mapping[str, FilterField],
        *,
        base: type[M] = BaseDto,
        doc: str | None = None,
) -> type[M]:
    """Create a request model with explicit ``field``/``field__operator`` filters.

    The resulting model can be used as a FastAPI ``Query()`` model or a JSON
    body. ``model.model_dump(exclude_none=True)`` produces filters suitable for
    :func:`grad_pylib.core.querying.apply_query`.
    """
    return create_model(
        model_name,
        __base__=base,
        __doc__=doc,
        **filter_field_definitions(fields),
    )


def filter_values(
        filters: BaseModel | None,
        model: type[BaseModel],
) -> dict[str, Any]:
    """Dump only the non-``None`` filter fields declared on ``model`` from ``filters``."""
    if filters is None:
        return {}
    return filters.model_dump(include=set(model.model_fields), exclude_none=True)
