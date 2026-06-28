from datetime import date, datetime
from decimal import Decimal
import types
from typing import Any, Union, get_origin, get_args

from pydantic import AliasChoices, AliasPath, BaseModel
from sqlalchemy import Table
from sqlalchemy.types import Boolean, Date, DateTime, Integer, Numeric, String

SQLA_TYPE_MAP = (
    (Integer, int),
    (String, str),
    (Boolean, bool),
    (DateTime, datetime),
    (Date, date),
    (Numeric, Decimal),
)


def _alias_names(alias: Any) -> list[str]:
    if alias is None:
        return []
    if isinstance(alias, str):
        return [alias]
    if isinstance(alias, AliasChoices):
        names: list[str] = []
        for choice in alias.choices:
            names.extend(_alias_names(choice))
        return names
    if isinstance(alias, AliasPath):
        return []
    return []


def _field_lookup_keys(name: str, field: Any) -> list[str]:
    keys = [name]
    for alias in (
            field.alias,
            field.validation_alias,
            field.serialization_alias,
    ):
        for candidate in _alias_names(alias):
            if candidate not in keys:
                keys.append(candidate)
    return keys


def _expected_python_type(column: Any) -> type[Any] | None:
    for sql_type, python_type in SQLA_TYPE_MAP:
        if isinstance(column.type, sql_type):
            return python_type
    return None


def assert_models_align(
        db_model: Any,
        api_model: type[BaseModel],
        ignore_fields: set[str] | None = None
):
    """
    Asserts that a SQLAlchemy model and a Pydantic API model have
    matching field names and compatible base types, ignoring nullability differences.
    """
    ignore_fields = ignore_fields or set()

    if isinstance(db_model, Table):
        db_columns = db_model.columns
    elif hasattr(db_model, "__table__"):
        db_columns = db_model.__table__.columns
    else:
        model_name = getattr(db_model, "__name__", type(db_model).__name__)
        raise TypeError(f"{model_name} is not a valid SQLAlchemy model or Table.")

    # 1. Extract Pydantic API fields and match explicit DB aliases when needed
    api_fields: dict[str, Any] = {}
    for name, field in api_model.model_fields.items():
        field_key = next(
        (key for key in _field_lookup_keys(name, field) if key in db_columns),
            name,
        )
        api_fields[field_key] = field.annotation

    # 2. Filter out explicitly ignored database fields
    active_db_keys = set(db_columns.keys()) - ignore_fields
    api_keys = set(api_fields.keys()) - ignore_fields

    # 3. Check for structural drift (Missing or Extra fields)
    missing_in_api = active_db_keys - api_keys
    extra_in_api = api_keys - active_db_keys

    assert not missing_in_api, f"Columns in SQLAlchemy not mapped in Pydantic: {missing_in_api}"
    assert not extra_in_api, f"Fields in Pydantic that don't exist in the DB model: {extra_in_api}"

    # 4. Check for Core Type Drift (Ignoring Optional/None wrappers)
    type_mismatches = []
    for field_key in active_db_keys:
        column = db_columns[field_key]
        expected_python_type = _expected_python_type(column)
        actual_pydantic_type = api_fields[field_key]

        # Safely unwrap Union/Optional types to isolate the base type (e.g., str | None -> str)
        origin = get_origin(actual_pydantic_type)
        if origin is Union or (hasattr(types, "UnionType") and origin is types.UnionType):
            inner_types = [t for t in get_args(actual_pydantic_type) if t is not type(None)]
            if len(inner_types) == 1:
                actual_pydantic_type = inner_types[0]

        # Check only if the core types don't match
        if expected_python_type and expected_python_type != actual_pydantic_type:
            type_mismatches.append(
                f"Field '{field_key}': DB base type is {expected_python_type.__name__}, "
                f"but Pydantic expects {getattr(actual_pydantic_type, '__name__', str(actual_pydantic_type))}"
            )

    assert not type_mismatches, "Type Drift Detected!\n" + "\n".join(type_mismatches)
