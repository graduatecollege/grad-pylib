from decimal import Decimal
from typing import Annotated

from pydantic import WithJsonSchema, PlainSerializer

OptionalStringDecimal = Annotated[
    Decimal | None,
    WithJsonSchema({"anyOf": [{"type": "string", "format": "big-decimal"}, {"type": "null"}]})
]

OptionalNumberDecimal = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda v: float(v) if v is not None else None,
        return_type=float | None,
        when_used="json"
    ),
    WithJsonSchema({"anyOf": [{"type": "number"}, {"type": "null"}]})
]

OptionalIntDecimal = Annotated[
    Decimal | None,
    PlainSerializer(
        lambda v: int(v) if v is not None else None,
        return_type=int | None,
        when_used="json"
    ),
    WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "null"}]})
]

StringDecimal = Annotated[
    Decimal,
    WithJsonSchema({"type": "string", "format": "big-decimal"})
]

NumberDecimal = Annotated[
    Decimal,
    PlainSerializer(
        lambda v: float(v),
        return_type=float,
        when_used="json"
    ),
    WithJsonSchema({"type": "number"})
]

IntDecimal = Annotated[
    Decimal,
    PlainSerializer(
        lambda v: int(v),
        return_type=int,
        when_used="json"
    ),
    WithJsonSchema({"type": "integer"})
]
