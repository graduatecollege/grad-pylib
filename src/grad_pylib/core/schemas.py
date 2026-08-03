from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from grad_pylib.core.time import utc_now


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
    return StatusResponse(
        status=status,
        app_name=app_name,
        version=version,
        timestamp=timestamp or utc_now(),
    )
