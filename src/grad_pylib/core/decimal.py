from decimal import Decimal
from typing import Annotated

from pydantic import WithJsonSchema, PlainSerializer

OptionalStringDecimal = Annotated[
    Decimal | None,
    WithJsonSchema({"anyOf": [{"type": "string", "format": "big-decimal"}, {"type": "null"}]})
]
"""Nullable precise decimal rendered as a string in the OpenAPI schema and JSON responses.

Use for values whose precision must survive clients that use IEEE 754 numbers, such as monetary
amounts. Pydantic still validates input as `Decimal`.
"""

OptionalNumberDecimal = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda v: float(v) if v is not None else None,
        return_type=float | None,
        when_used="json"
    ),
    WithJsonSchema({"anyOf": [{"type": "number"}, {"type": "null"}]})
]
"""Nullable decimal rendered as a JSON number for APIs that accept floating-point precision loss."""

OptionalIntDecimal = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda v: int(v) if v is not None else None,
        return_type=int | None,
        when_used="json"
    ),
    WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "null"}]})
]
"""Nullable decimal rendered as an integer, truncating any fractional portion on JSON output."""

StringDecimal = Annotated[
    Decimal,
    WithJsonSchema({"type": "string", "format": "big-decimal"})
]
"""Required precise decimal rendered as a string to preserve its exact value for API clients."""

NumberDecimal = Annotated[
    Decimal,
    PlainSerializer(
        lambda v: float(v),
        return_type=float,
        when_used="json"
    ),
    WithJsonSchema({"type": "number"})
]
"""Required decimal rendered as a JSON number when float conversion is acceptable."""

IntDecimal = Annotated[
    Decimal,
    PlainSerializer(
        lambda v: int(v),
        return_type=int,
        when_used="json"
    ),
    WithJsonSchema({"type": "integer"})
]
"""Required decimal rendered as an integer, truncating any fractional portion on JSON output."""
