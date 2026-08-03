import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


from grad_pylib.core.time import utc_now


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    distinct: list[str] = []
    for value in values:
        if value in seen:
            continue
        distinct.append(value)
        seen.add(value)
    return distinct


def parse_comma_separated_strings(
    value: str | Iterable[object] | None,
    *,
    dedupe: bool = False,
    sort: bool = False,
) -> list[str]:
    """
    Parse a comma-separated string or iterable of values into a clean string list.

    Blank items are removed and surrounding whitespace is stripped. Duplicates and sorting are
    caller-controlled so application validators can keep their business semantics explicit.
    """
    if value is None:
        return []

    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, Iterable) and not isinstance(value, Mapping | bytes | bytearray):
        raw_items = value
    else:
        raise ValueError("Expected a comma-separated string, an iterable of values, or None.")

    parsed = [
        normalized
        for item in raw_items
        if item is not None and (normalized := str(item).strip())
    ]
    if dedupe:
        parsed = _dedupe_preserving_order(parsed)
    if sort:
        parsed = sorted(parsed)
    return parsed


def validate_string_items[T](
    values: Iterable[str],
    *,
    validator: Callable[[str], T],
) -> list[T]:
    """Apply a caller-provided validator to each item in a parsed string list."""
    return [validator(value) for value in values]


def parse_validated_comma_separated_strings[T](
    value: str | Iterable[object] | None,
    *,
    validator: Callable[[str], T],
    dedupe: bool = False,
    sort: bool = False,
) -> list[T]:
    """Parse a comma-separated string field and validate or normalize each item."""
    return validate_string_items(
        parse_comma_separated_strings(value, dedupe=dedupe, sort=sort),
        validator=validator,
    )


def parse_json_blob(
    value: Any,
    *,
    invalid_to_none: bool = False,
) -> Any | None:
    """Parse JSON when the input is a string, otherwise pass through the existing value."""
    if value is None or not isinstance(value, str):
        return value

    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        if invalid_to_none:
            return None
        raise ValueError("Invalid JSON.") from exc


def normalize_email_list(
    value: str | Iterable[object] | None,
    *,
    dedupe: bool = False,
    sort: bool = False,
    require_non_empty: bool = False,
) -> list[str]:
    """Trim, lowercase, and optionally deduplicate a list of email values."""
    parsed = [email.lower() for email in parse_comma_separated_strings(value)]
    if dedupe:
        parsed = _dedupe_preserving_order(parsed)
    if sort:
        parsed = sorted(parsed)
    if require_non_empty and not parsed:
        raise ValueError("Expected at least one email address.")
    return parsed


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="ignore",
    )


class DataResponse[T](CamelModel):
    data: T


class ItemResponse[T](DataResponse[T]):
    pass


class ListResponse[T](DataResponse[list[T]]):
    pass


class MetaResponse[T, M](DataResponse[T]):
    meta: M


class StatusResponse(CamelModel):
    status: str = "ok"
    app_name: str | None = None
    version: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)


def build_status_response(
    *,
    app_name: str | None = None,
    version: str | None = None,
    status: str = "ok",
    timestamp: datetime | None = None,
) -> StatusResponse:
    if timestamp is None:
        ts = utc_now()
    elif timestamp.tzinfo is not None:
        ts = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        ts = timestamp
    return StatusResponse(
        status=status,
        app_name=app_name,
        version=version,
        timestamp=ts,
    )
